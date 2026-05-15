"""
Real-time market watcher.
- Subscribes to 1-min K-lines + real-time quotes (QUOTE push)
- Runs full TA analysis every 5 seconds per ticker
- Detects: ORB Breakout, VWAP Bounce, Momentum Surge, TA Signal (score ≥ 6)
- Calls day_trader.on_signal() when a condition fires
"""
import threading
import time
from datetime import datetime

try:
    import pytz
    HK_TZ = pytz.timezone('Asia/Hong_Kong')
except ImportError:
    HK_TZ = None

try:
    from futu import (
        OpenQuoteContext, CurKlineHandlerBase, StockQuoteHandlerBase,
        SubType, RET_OK,
    )
    FUTU_OK = True
except ImportError:
    FUTU_OK = False

HOST, PORT = '127.0.0.1', 11111
_signal_callback = None
_watched_codes   = []

# TA_SIGNAL / TA_SIGNAL_SHORT disabled: 0% win rate across all trades, worst signal type (-3.49% combined)
DISABLED_SIGNAL_TYPES: set = {"TA_SIGNAL", "TA_SIGNAL_SHORT"}
_ta_results: dict = {}        # code → latest TA dict
_last_ta_time: dict = {}      # code → last analysis timestamp


# ── Intraday state per ticker ─────────────────────────────────

class _TickerState:
    def __init__(self, code):
        self.code          = code
        self.candles: list = []
        self.orb_high      = None
        self.orb_low       = None
        self.orb_done      = False
        self.vwap_pv       = 0.0
        self.vwap_v        = 0.0
        self.current_price = 0.0
        self.fired: set    = set()

    @property
    def vwap(self):
        return self.vwap_pv / self.vwap_v if self.vwap_v else 0.0

    @property
    def avg_vol(self):
        n = min(len(self.candles), 20)
        return sum(c["v"] for c in self.candles[-n:]) / n if n else 1

    def add_candle(self, o, h, l, c, v):
        self.candles.append({"o": o, "h": h, "l": l, "c": c, "v": v})
        self.vwap_pv += ((h + l + c) / 3) * v
        self.vwap_v  += v
        self.current_price = c

        if len(self.candles) <= 30:
            if self.orb_high is None:
                self.orb_high, self.orb_low = h, l
            else:
                self.orb_high = max(self.orb_high, h)
                self.orb_low  = min(self.orb_low,  l)
            if len(self.candles) == 30:
                self.orb_done = True
                print(f"[watcher] {self.code} ORB: {self.orb_low:.2f}–{self.orb_high:.2f}")

    def _trend_aligned(self, direction: str, window: int = 5) -> bool:
        """Return True if 3 of the last 5 one-min candles agree with direction."""
        if len(self.candles) < window:
            return True  # not enough data, allow through
        recent = self.candles[-window:]
        bullish = sum(1 for c in recent if c["c"] > c["o"])
        if direction == "BUY":
            return bullish >= 3
        else:
            return (window - bullish) >= 3

    def _rsi2(self) -> float:
        """RSI(2) — overbought/oversold filter for VWAP bounces."""
        closes = [c["c"] for c in self.candles]
        if len(closes) < 3:
            return 50.0
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        ag = sum(gains[-2:]) / 2
        al = sum(losses[-2:]) / 2
        return round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0

    def _rsi(self, period: int = 3) -> float:
        """Generic RSI for any period — default RSI(3) matches claude-execute strategy."""
        closes = [c["c"] for c in self.candles]
        if len(closes) < period + 1:
            return 50.0
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        ag = sum(gains[-period:]) / period
        al = sum(losses[-period:]) / period
        return round(100 - 100 / (1 + ag / al), 2) if al > 0 else 100.0

    def _ema(self, period: int = 8) -> float:
        """EMA(period) over close prices."""
        closes = [c["c"] for c in self.candles]
        if len(closes) < period:
            return closes[-1] if closes else 0.0
        mult = 2 / (period + 1)
        ema = sum(closes[:period]) / period
        for c in closes[period:]:
            ema = c * mult + ema * (1 - mult)
        return round(ema, 3)

    def get_classic_signals(self) -> list:
        if len(self.candles) < 2:
            return []
        signals = []
        last, prev = self.candles[-1], self.candles[-2]
        vol_ratio = last["v"] / self.avg_vol if self.avg_vol else 1

        # Detect candle patterns for confirmation gate
        try:
            from technical_analysis import detect_candlestick_patterns
            c_pats = detect_candlestick_patterns(self.candles)
        except Exception:
            c_pats = []
        bull_marubozu = any(p["name"] == "MARUBOZU" and p.get("direction") == "BULL" for p in c_pats)
        bear_marubozu = any(p["name"] == "MARUBOZU" and p.get("direction") == "BEAR" for p in c_pats)
        pat_names     = {p["name"] for p in c_pats}
        has_bull = bool(pat_names & {"BULL_ENGULFING", "MORNING_STAR", "HAMMER"}) or bull_marubozu
        has_bear = bool(pat_names & {"BEAR_ENGULFING", "EVENING_STAR", "SHOOTING_STAR"}) or bear_marubozu

        # ORB Breakout — 30-min range, RVOL ≥ 2.0, VWAP-aligned + bullish candle confirmation
        if self.orb_done and len(self.candles) > 30:
            key = f"ORB_{len(self.candles)}"
            if key not in self.fired and vol_ratio >= 2.0 and self._trend_aligned("BUY") and has_bull:
                vwap_ok = self.vwap <= 0 or last["c"] > self.vwap
                if prev["c"] <= self.orb_high < last["c"] and vwap_ok:
                    orb_range = self.orb_high - self.orb_low
                    entry = last["c"]
                    stop  = round(max(self.orb_low, entry * 0.99), 3)
                    target = round(entry + orb_range * 2, 3)
                    pat_tag = "+".join(sorted(pat_names & {"BULL_ENGULFING", "MORNING_STAR", "HAMMER", "MARUBOZU"}))
                    signals.append({
                        "type": "ORB_BREAKOUT", "direction": "BUY",
                        "price": entry, "stop": stop, "target": target,
                        "reason": f"ORB突破 {self.orb_high:.2f}＋{pat_tag}，RVOL {vol_ratio:.1f}x",
                        "vol_ratio": vol_ratio,
                    })
                    self.fired.add(key)

        # VWAP Bounce — requires bullish reversal candle at VWAP level
        if self.vwap > 0 and len(self.candles) > 20:
            dist_now  = (last["c"] - self.vwap) / self.vwap * 100
            dist_prev = (prev["c"] - self.vwap) / self.vwap * 100
            key = f"VWAP_{len(self.candles)}"
            if key not in self.fired and vol_ratio > 1.5 and self._trend_aligned("BUY") and has_bull:
                rsi2 = self._rsi2()
                if dist_prev < -0.3 and dist_now > -0.1 and rsi2 < 30:
                    entry = last["c"]
                    stop  = round(entry * 0.985, 3)
                    target = round(entry * 1.03, 3)
                    pat_tag = "+".join(sorted(pat_names & {"BULL_ENGULFING", "MORNING_STAR", "HAMMER", "MARUBOZU"}))
                    signals.append({
                        "type": "VWAP_BOUNCE", "direction": "BUY",
                        "price": entry, "stop": stop, "target": target,
                        "reason": f"VWAP {self.vwap:.2f} 反彈＋{pat_tag}，RSI(2)={rsi2:.0f}，量比 {vol_ratio:.1f}x",
                        "vol_ratio": vol_ratio,
                    })
                    self.fired.add(key)

        # Momentum Surge — requires bullish MARUBOZU (full-body conviction candle)
        if len(self.candles) >= 5:
            window   = self.candles[-5:]
            move_pct = (window[-1]["c"] - window[0]["o"]) / window[0]["o"] * 100
            cum_vol  = sum(c["v"] for c in window)
            key = f"MOM_{len(self.candles)}"
            if (key not in self.fired and move_pct > 1.2 and cum_vol > self.avg_vol * 1.6 * 5
                    and self._trend_aligned("BUY") and (bull_marubozu or has_bull)):
                signals.append({
                    "type": "MOMENTUM_SURGE", "direction": "BUY",
                    "price": last["c"],
                    "stop":  round(last["c"] * 0.985, 3),
                    "target": round(last["c"] * 1.03, 3),
                    "reason": f"5分鐘動量 +{move_pct:.1f}%＋蠟燭確認，累量 {cum_vol/self.avg_vol:.1f}x",
                    "vol_ratio": cum_vol / (self.avg_vol * 5),
                })
                self.fired.add(key)

        # ORB Breakdown (bearish) — requires bearish candle confirmation
        if self.orb_done and len(self.candles) > 30:
            key = f"ORB_SHORT_{len(self.candles)}"
            if key not in self.fired and vol_ratio >= 2.0 and self._trend_aligned("SELL") and has_bear:
                vwap_ok = self.vwap <= 0 or last["c"] < self.vwap
                if prev["c"] >= self.orb_low > last["c"] and vwap_ok:
                    orb_range = self.orb_high - self.orb_low
                    entry = last["c"]
                    stop  = round(min(self.orb_high, entry * 1.01), 3)
                    target = round(entry - orb_range * 2, 3)
                    pat_tag = "+".join(sorted(pat_names & {"BEAR_ENGULFING", "EVENING_STAR", "SHOOTING_STAR", "MARUBOZU"}))
                    signals.append({
                        "type": "ORB_BREAKDOWN", "direction": "SELL",
                        "price": entry, "stop": stop, "target": target,
                        "reason": f"ORB跌穿 {self.orb_low:.2f}＋{pat_tag}，RVOL {vol_ratio:.1f}x",
                        "vol_ratio": vol_ratio,
                    })
                    self.fired.add(key)

        # VWAP Rejection (bearish) — requires bearish reversal candle
        if self.vwap > 0 and len(self.candles) > 20:
            dist_now  = (last["c"] - self.vwap) / self.vwap * 100
            dist_prev = (prev["c"] - self.vwap) / self.vwap * 100
            key = f"VWAP_REJ_{len(self.candles)}"
            if key not in self.fired and vol_ratio > 1.5 and self._trend_aligned("SELL") and has_bear:
                rsi2_r = self._rsi2()
                if dist_prev > 0.3 and dist_now < 0.1 and rsi2_r > 70:
                    entry  = last["c"]
                    stop   = round(entry * 1.015, 3)
                    target = round(entry * 0.97, 3)
                    pat_tag = "+".join(sorted(pat_names & {"BEAR_ENGULFING", "EVENING_STAR", "SHOOTING_STAR", "MARUBOZU"}))
                    signals.append({
                        "type": "VWAP_REJECTION", "direction": "SELL",
                        "price": entry, "stop": stop, "target": target,
                        "reason": f"VWAP {self.vwap:.2f} 拒絕＋{pat_tag}，RSI(2)={rsi2_r:.0f}，量比 {vol_ratio:.1f}x",
                        "vol_ratio": vol_ratio,
                    })
                    self.fired.add(key)

        # Momentum Drop (bearish) — requires bearish MARUBOZU
        if len(self.candles) >= 5:
            window   = self.candles[-5:]
            move_pct = (window[-1]["c"] - window[0]["o"]) / window[0]["o"] * 100
            cum_vol  = sum(c["v"] for c in window)
            key = f"MOM_SHORT_{len(self.candles)}"
            if (key not in self.fired and move_pct < -1.2 and cum_vol > self.avg_vol * 1.6 * 5
                    and self._trend_aligned("SELL") and (bear_marubozu or has_bear)):
                signals.append({
                    "type": "MOMENTUM_DROP", "direction": "SELL",
                    "price": last["c"],
                    "stop":  round(last["c"] * 1.015, 3),
                    "target": round(last["c"] * 0.97, 3),
                    "reason": f"5分鐘急跌 {move_pct:.1f}%＋蠟燭確認，累量 {cum_vol/self.avg_vol:.1f}x",
                    "vol_ratio": cum_vol / (self.avg_vol * 5),
                })
                self.fired.add(key)

        return signals


_states: dict[str, _TickerState] = {}

def _get_state(code) -> _TickerState:
    if code not in _states:
        _states[code] = _TickerState(code)
    return _states[code]


# ── Futu handlers ─────────────────────────────────────────────

class _KLineHandler(CurKlineHandlerBase):
    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != RET_OK or data is None or data.empty:
            return ret, data
        for _, row in data.iterrows():
            code  = row["code"]
            state = _get_state(code)
            state.add_candle(
                o=float(row["open"]), h=float(row["high"]),
                l=float(row["low"]),  c=float(row["close"]),
                v=float(row.get("volume", 0)),
            )
            for sig in state.get_classic_signals():
                sig["code"] = code
                _enrich_and_fire(sig, state)
        return ret, data

class _QuoteHandler(StockQuoteHandlerBase):
    def on_recv_rsp(self, rsp_pb):
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != RET_OK or data is None or data.empty:
            return ret, data
        for _, row in data.iterrows():
            code = row["code"]
            try:
                price = float(row["last_price"])
                if price > 0:
                    _get_state(code).current_price = price
            except:
                pass
        return ret, data


def _enrich_and_fire(sig: dict, state: _TickerState):
    """Attach TA data + safety-check indicators to signal and call the callback."""
    ta = _ta_results.get(sig["code"], {})
    sig["ta_score"]        = ta.get("score", 0)
    sig["candle_patterns"] = ta.get("candle_patterns", [])
    sig["chart_patterns"]  = ta.get("chart_patterns", [])
    # Safety-check indicators (claude-execute strategy)
    sig["vwap"]  = round(state.vwap, 3)
    sig["ema8"]  = state._ema(8)
    sig["rsi3"]  = state._rsi(3)
    sig["rsi14"] = state._rsi(14)
    if _signal_callback:
        _signal_callback(sig)


# ── Candle-pattern-at-level signal (primary strategy) ─────────

def _detect_candle_signal(state: _TickerState, result: dict) -> dict | None:
    """
    Primary signal: strong candle pattern forms at a key intraday level with volume.
    Pattern (what) + level (where) + volume (conviction) = CANDLE_PATTERN_SIGNAL.
    """
    c_pats = result.get("candle_patterns", [])
    if not c_pats:
        return None

    price  = state.current_price or result.get("price", 0)
    vwap   = state.vwap
    sup    = result.get("support")
    res    = result.get("resistance")
    atr    = result.get("indicators", {}).get("atr")
    score  = result.get("score", 0)
    vol    = (state.candles[-1]["v"] / state.avg_vol
              if state.avg_vol and state.candles else 1.0)

    if price <= 0 or vol < 1.5:
        return None

    BULL_PATS = {"BULL_ENGULFING", "MORNING_STAR", "HAMMER"}
    BEAR_PATS = {"BEAR_ENGULFING", "EVENING_STAR", "SHOOTING_STAR"}

    bull_names = {p["name"] for p in c_pats
                  if p["name"] in BULL_PATS or
                  (p["name"] == "MARUBOZU" and p.get("direction") == "BULL")}
    bear_names = {p["name"] for p in c_pats
                  if p["name"] in BEAR_PATS or
                  (p["name"] == "MARUBOZU" and p.get("direction") == "BEAR")}

    if not bull_names and not bear_names:
        return None

    # Level proximity 1.5% — pattern must form near a meaningful level, not in mid-air
    at_vwap_bull  = vwap > 0 and abs(price - vwap) / vwap < 0.015 and price >= vwap * 0.995
    at_vwap_bear  = vwap > 0 and abs(price - vwap) / vwap < 0.015 and price <= vwap * 1.005
    at_support    = sup and sup > 0 and abs(price - sup) / price < 0.015
    at_resistance = res and res > 0 and abs(price - res) / price < 0.015

    stop_dist   = atr * 3 if atr else price * 0.015
    target_dist = atr * 6 if atr else price * 0.03  # 2:1 R/R minimum

    if bull_names and (at_vwap_bull or at_support):
        level_tag = f"VWAP {vwap:.2f}" if at_vwap_bull else f"支撐 {sup:.2f}"
        return {
            "type":    "CANDLE_PATTERN_SIGNAL",
            "direction": "BUY",
            "price":   price,
            "stop":    round(price - stop_dist, 3),
            "target":  round(price + target_dist, 3),
            "reason":  f"蠟燭型態 {'＋'.join(sorted(bull_names))} 於 {level_tag} 形成，量比 {vol:.1f}x",
            "vol_ratio": round(vol, 2),
            "ta_score":  score,
            "candle_patterns": c_pats,
            "chart_patterns":  result.get("chart_patterns", []),
        }

    if bear_names and (at_vwap_bear or at_resistance):
        level_tag = f"VWAP {vwap:.2f}" if at_vwap_bear else f"阻力 {res:.2f}"
        return {
            "type":    "CANDLE_PATTERN_SHORT",
            "direction": "SELL",
            "price":   price,
            "stop":    round(price + stop_dist, 3),
            "target":  round(price - target_dist, 3),
            "reason":  f"蠟燭型態 {'＋'.join(sorted(bear_names))} 於 {level_tag} 形成，量比 {vol:.1f}x",
            "vol_ratio": round(vol, 2),
            "ta_score":  score,
            "candle_patterns": c_pats,
            "chart_patterns":  result.get("chart_patterns", []),
        }

    return None


# ── 5-second TA analysis loop ─────────────────────────────────

def _ta_loop():
    """Runs every 30 seconds, performs full TA on all watched tickers."""
    import technical_analysis as ta_mod
    while True:
        time.sleep(30)
        for code, state in list(_states.items()):
            if len(state.candles) < 5:
                continue
            try:
                result = ta_mod.analyze(code, state.candles,
                                        state.current_price or None)
                _ta_results[code] = result

                # CANDLE_PATTERN_SIGNAL — primary strategy: pattern at key level
                key_cp = f"CP_{len(state.candles)}"
                if key_cp not in state.fired:
                    cp_sig = _detect_candle_signal(state, result)
                    if cp_sig:
                        cp_sig["code"] = code
                        state.fired.add(key_cp)
                        if _signal_callback:
                            _signal_callback(cp_sig)

                # Bullish TA_SIGNAL
                key = f"TA_{len(state.candles)}"
                if key not in state.fired and result["score"] >= 5 and "TA_SIGNAL" not in DISABLED_SIGNAL_TYPES:
                    price = result["price"]
                    sup   = result.get("support")
                    res   = result.get("resistance")
                    # Clamp stop strictly below entry, target strictly above
                    stop   = min(sup, price * 0.985) if sup and sup < price else round(price * 0.985, 3)
                    target = max(res, price * 1.03)  if res and res > price else round(price * 1.03, 3)
                    sig = {
                        "code":    code,
                        "type":    "TA_SIGNAL",
                        "direction": "BUY",
                        "price":   price,
                        "stop":    round(stop, 3),
                        "target":  round(target, 3),
                        "reason":  f"TA 評分 {result['score']}/10：{', '.join(result['signals'][:3])}",
                        "vol_ratio": result["volume"]["ratio"],
                        "ta_score":  result["score"],
                        "candle_patterns": result["candle_patterns"],
                        "chart_patterns":  result["chart_patterns"],
                    }
                    state.fired.add(key)
                    if _signal_callback:
                        _signal_callback(sig)

                # Bearish TA_SIGNAL
                key_s = f"TA_SHORT_{len(state.candles)}"
                if key_s not in state.fired and result["score"] <= -5 and "TA_SIGNAL_SHORT" not in DISABLED_SIGNAL_TYPES:
                    price = result["price"]
                    sup   = result.get("support")
                    res   = result.get("resistance")
                    # Clamp stop strictly above entry, target strictly below
                    stop   = max(res, price * 1.015) if res and res > price else round(price * 1.015, 3)
                    target = min(sup, price * 0.97)  if sup and sup < price else round(price * 0.97, 3)
                    sig_s = {
                        "code":    code,
                        "type":    "TA_SIGNAL_SHORT",
                        "direction": "SELL",
                        "price":   price,
                        "stop":    round(stop, 3),
                        "target":  round(target, 3),
                        "reason":  f"TA 評分 {result['score']}/10（看跌）：{', '.join(result['signals'][:3])}",
                        "vol_ratio": result["volume"]["ratio"],
                        "ta_score":  result["score"],
                        "candle_patterns": result["candle_patterns"],
                        "chart_patterns":  result["chart_patterns"],
                    }
                    state.fired.add(key_s)
                    if _signal_callback:
                        _signal_callback(sig_s)
            except Exception as e:
                print(f"[watcher] TA error for {code}: {e}")


# ── Market hours ──────────────────────────────────────────────

def is_hk_market_open() -> bool:
    if HK_TZ is None:
        return True
    now = datetime.now(HK_TZ)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= t < 12 * 60) or (13 * 60 <= t < 16 * 60)

def is_us_market_open() -> bool:
    """US ET 9:30–16:00 = UTC 14:30–21:00 (adjust for DST via pytz)."""
    try:
        import pytz
        et  = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if now.weekday() >= 5:
            return False
        t = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= t < 16 * 60
    except Exception:
        return True


# ── Public API ────────────────────────────────────────────────

def start_watcher(codes: list, on_signal, market: str = "HK"):
    """
    market: "HK" or "US" — controls which market-hours gate to use.
    Can be called twice (once for HK codes, once for US codes).
    """
    global _signal_callback, _watched_codes
    _signal_callback = on_signal
    _watched_codes   = codes

    if not FUTU_OK:
        print("[watcher] futu SDK not found — watcher disabled")
        return

    # Start 5-second TA analysis thread once (shared across both markets)
    if not any(t.name == "ta-loop" for t in threading.enumerate()):
        threading.Thread(target=_ta_loop, daemon=True, name="ta-loop").start()

    is_open_fn = is_us_market_open if market == "US" else is_hk_market_open

    def _loop():
        ctx = None
        label = f"[watcher-{market}]"
        while True:
            try:
                if not is_open_fn():
                    if ctx:
                        ctx.close(); ctx = None
                    time.sleep(60)
                    continue

                if ctx is None:
                    print(f"{label} Connecting for {codes}...")
                    ctx = OpenQuoteContext(host=HOST, port=PORT)
                    ctx.set_handler(_KLineHandler())
                    ctx.set_handler(_QuoteHandler())
                    ret, msg = ctx.subscribe(
                        codes, [SubType.K_1M, SubType.QUOTE],
                        subscribe_push=True
                    )
                    if ret != RET_OK:
                        print(f"{label} Subscribe failed: {msg}")
                        ctx.close(); ctx = None
                        time.sleep(10)
                        continue
                    print(f"{label} Subscribed (K_1M + QUOTE) for {codes}")

                time.sleep(5)

            except Exception as e:
                print(f"{label} Error: {e}")
                if ctx:
                    try: ctx.close()
                    except: pass
                ctx = None
                time.sleep(15)

    threading.Thread(target=_loop, daemon=True, name=f"watcher-{market}").start()
    print(f"[watcher-{market}] Started")

def get_ta_results() -> dict:
    return dict(_ta_results)

def reset_daily():
    _states.clear()
    _ta_results.clear()
    _last_ta_time.clear()
    print("[watcher] Daily state reset")
