"""
Day Trading Engine.
- 30% of capital (HKD 300,000) reserved for day trading
- 70% (HKD 700,000) reserved for long-term
- Weekly target: +5% of day trade pool = HKD 15,000 → auto-pause until Monday
- Fault tolerance: -10% of day trade pool = -HKD 30,000 → emergency stop + immediate alert
- Max 3 concurrent intraday positions (HKD 100,000 each)
"""
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta

try:
    from futu import (
        OpenSecTradeContext, OpenQuoteContext,
        TrdMarket, TrdEnv, TrdSide, OrderType, RET_OK,
    )
    FUTU_OK = True
except ImportError:
    FUTU_OK = False

HOST, PORT = '127.0.0.1', 11111

# ── Capital allocation ────────────────────────────────────────
TOTAL_CAPITAL_HKD      = 1_000_000
DAY_TRADE_POOL_HKD     = 300_000    # 30%
LONG_TERM_POOL_HKD     = 700_000    # 70%
WEEKLY_TARGET_HKD      = 15_000     # 5% of day trade pool
FAULT_TOLERANCE_HKD    = 90_000     # 30% of day trade pool
MAX_INTRADAY_POSITIONS = 3
PER_TRADE_BUDGET_HKD   = DAY_TRADE_POOL_HKD // MAX_INTRADAY_POSITIONS  # 100,000
TRAIL_PCT              = 0.02    # fallback trailing stop 2% (used if ATR unavailable)
TRAIL_ATR_MULT         = 3.0     # initial trailing stop = 3× ATR (wider to survive 1-min noise)
TRAIL_ATR_MULT_TIGHT   = 2.0     # tightens to 2× ATR after MIN_HOLD_SECS
MIN_HOLD_SECS          = 180     # 3-minute minimum hold before stops checked (avoids entry-candle noise)
TIME_STOP_MINUTES      = 45      # close flat positions after 45 min
TIME_STOP_MIN_MOVE_PCT = 0.005   # "flat" = < 0.5% in our favour
NO_NEW_ENTRY_MINS      = 45      # block new entries in last 45 min before close

STARTUP_WARMUP_SECS    = 60      # ignore signals for 60s after startup (historical replay)

WEEKLY_TRACKER_FILE = os.path.join(os.path.dirname(__file__), "weekly_tracker.json")

_day_trade_enabled   = True
_open_intraday: list = []
_lock                = threading.Lock()
_consecutive_losses  = 0
_start_time          = time.time()
_intraday_paused     = False
_cooldown: dict      = {}   # code → expiry timestamp
COOLDOWN_SECS        = 20 * 60

_stats_cache:       dict  = {}
_stats_cache_time:  float = 0.0
STATS_CACHE_TTL           = 300  # refresh every 5 minutes

_southbound_flow_hkd: float = 0.0   # set at 09:35 HKT each day
SOUTHBOUND_THRESHOLD        = 500   # HKD millions — strong flow = bias signal

_KNOWN_SHORTABLE = {
    # HK
    "HK.00700", "HK.09988", "HK.03690", "HK.09618",
    "HK.00005", "HK.00388", "HK.00941", "HK.09999",
    "HK.02318", "HK.01211",
    # US — major liquid stocks are all shortable on Futu simulate
    "US.AAPL", "US.NVDA", "US.TSLA", "US.AMZN", "US.GOOGL",
    "US.META", "US.MSFT", "US.ORCL", "US.JPM", "US.V",
}

_SECTOR_MAP = {
    # HK
    "HK.00700": "科技", "HK.09988": "科技", "HK.03690": "科技",
    "HK.09618": "科技", "HK.09999": "科技",
    "HK.00005": "金融", "HK.00388": "金融", "HK.02318": "金融",
    "HK.00941": "電訊",
    "HK.01211": "新能源",
    # US
    "US.AAPL": "科技", "US.NVDA": "科技", "US.MSFT": "科技",
    "US.GOOGL": "科技", "US.META": "科技", "US.ORCL": "科技",
    "US.TSLA": "消費", "US.AMZN": "消費",
    "US.JPM": "金融", "US.V": "金融",
}
MAX_SAME_SECTOR = 2

USD_HKD = 7.8  # FX conversion for position sizing


# ── Weekly P&L Tracker ────────────────────────────────────────

class _WeeklyTracker:
    def __init__(self):
        self._reload()

    def _reload(self):
        HKT    = timezone(timedelta(hours=8))
        now    = datetime.now(HKT)
        monday = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        data   = {}
        if os.path.exists(WEEKLY_TRACKER_FILE):
            try:
                with open(WEEKLY_TRACKER_FILE) as f:
                    data = json.load(f)
            except:
                pass

        if data.get("week_start") != monday:
            self.week_start        = monday
            self.realized_pnl_hkd  = 0.0
            self.paused_target_hit = False
            self.emergency_stopped = False
            self._save()
            if data.get("week_start"):
                print(f"[weekly] 新一週開始，重置週度 P&L 紀錄")
        else:
            self.week_start        = data["week_start"]
            self.realized_pnl_hkd  = data.get("realized_pnl_hkd", 0.0)
            self.paused_target_hit = data.get("paused_target_hit", False)
            self.emergency_stopped = data.get("emergency_stopped", False)

    def _save(self):
        with open(WEEKLY_TRACKER_FILE, "w") as f:
            json.dump({
                "week_start":        self.week_start,
                "realized_pnl_hkd":  self.realized_pnl_hkd,
                "paused_target_hit": self.paused_target_hit,
                "emergency_stopped": self.emergency_stopped,
            }, f, indent=2)

    def add_pnl(self, amount_hkd: float):
        self.realized_pnl_hkd = round(self.realized_pnl_hkd + amount_hkd, 2)
        self._save()
        self._check_limits()

    def _check_limits(self):
        global _day_trade_enabled
        if self.realized_pnl_hkd >= WEEKLY_TARGET_HKD and not self.paused_target_hit:
            self.paused_target_hit = True
            _day_trade_enabled     = False
            self._save()
            _notify(
                f"🎯 <b>週度目標達成！</b>\n"
                f"本週已實現盈利：<b>HKD {self.realized_pnl_hkd:,.0f}</b>\n"
                f"目標：HKD {WEEKLY_TARGET_HKD:,.0f}（+5%）\n\n"
                f"✅ 日內交易已自動暫停，下週一自動重啟。好好休息！"
            )
            print(f"[weekly] 🎯 目標達成 HKD {self.realized_pnl_hkd:.0f}，暫停日內交易")

        if self.realized_pnl_hkd <= -FAULT_TOLERANCE_HKD and not self.emergency_stopped:
            self.emergency_stopped = True
            _day_trade_enabled     = False
            self._save()
            _notify(
                f"🚨 <b>緊急警報！虧損超出容錯上限！</b>\n\n"
                f"本週虧損：<b>HKD {abs(self.realized_pnl_hkd):,.0f}</b>\n"
                f"容錯上限：HKD {FAULT_TOLERANCE_HKD:,.0f}（-10%）\n\n"
                f"🔴 日內交易已緊急停止。請立即查閱帳戶並決定下一步！"
            )
            print(f"[weekly] 🚨 緊急停止！本週虧損 HKD {abs(self.realized_pnl_hkd):.0f}")

    def can_trade(self) -> tuple:
        self._reload()  # always reload to catch Monday resets
        if self.emergency_stopped:
            return False, f"緊急停止：本週虧損 HKD {abs(self.realized_pnl_hkd):,.0f} 超過容錯上限"
        if self.paused_target_hit:
            return False, f"週度目標已達成，等待下週一重啟"
        return True, ""

    def get_state(self) -> dict:
        self._reload()
        pct = self.realized_pnl_hkd / DAY_TRADE_POOL_HKD * 100
        return {
            "week_start":           self.week_start,
            "realized_pnl_hkd":     self.realized_pnl_hkd,
            "pct_of_pool":          round(pct, 2),
            "weekly_target_hkd":    WEEKLY_TARGET_HKD,
            "fault_tolerance_hkd":  FAULT_TOLERANCE_HKD,
            "paused_target_hit":    self.paused_target_hit,
            "emergency_stopped":    self.emergency_stopped,
            "day_trade_pool_hkd":   DAY_TRADE_POOL_HKD,
            "long_term_pool_hkd":   LONG_TERM_POOL_HKD,
            "per_trade_budget_hkd": PER_TRADE_BUDGET_HKD,
        }

_weekly = _WeeklyTracker()


# ── HSI trend filter ─────────────────────────────────────────

def _get_disabled_signals() -> set:
    """Return signal types whose win rate < 35% with ≥ 10 trades."""
    global _stats_cache, _stats_cache_time
    if time.time() - _stats_cache_time > STATS_CACHE_TTL:
        try:
            from trade_journal import get_stats
            _stats_cache = get_stats()
        except:
            pass
        _stats_cache_time = time.time()
    disabled = set()
    for sig_type, data in _stats_cache.get("by_signal", {}).items():
        if data["trades"] >= 10 and data["wins"] / data["trades"] < 0.35:
            disabled.add(sig_type)
            print(f"[day_trader] {sig_type} 勝率 {data['wins']}/{data['trades']} 過低，已停用")
    return disabled


def _is_shortable(code: str) -> bool:
    # In simulate mode all stocks can be shorted; no margin restriction applies.
    return True


def _hsi_change_pct() -> float:
    """Returns HSI % change from open. 0.0 if unavailable."""
    try:
        with OpenQuoteContext(host=HOST, port=PORT) as qctx:
            ret, data = qctx.get_market_snapshot(["HK.HSI"])
            if ret == RET_OK and not data.empty:
                row = data.iloc[0]
                open_p = float(row.get("open_price", 0))
                last_p = float(row.get("last_price", 0))
                if open_p > 0:
                    return (last_p - open_p) / open_p * 100
    except:
        pass
    return 0.0


# ── Futu helpers ──────────────────────────────────────────────

def _get_sim_acc_id(ctx) -> int:
    ret, data = ctx.get_acc_list()
    if ret != RET_OK or data.empty:
        return 0
    sim = data[data['trd_env'] == 'SIMULATE']
    return int(sim['acc_id'].iloc[0]) if not sim.empty else 0

def _get_available_cash(ctx, acc_id: int) -> float:
    """Return available cash in the simulate account (HKD)."""
    try:
        ret, info = ctx.accinfo_query(trd_env=TrdEnv.SIMULATE, acc_id=acc_id)
        if ret == RET_OK and not info.empty:
            cash = float(info['cash'].iloc[0] or 0)
            return max(cash, 0.0)
    except:
        pass
    return 0.0

def _get_lot_size(code: str) -> int:
    try:
        with OpenQuoteContext(host=HOST, port=PORT) as qctx:
            ret, data = qctx.get_market_snapshot([code])
            if ret == RET_OK and not data.empty:
                return int(data['lot_size'].iloc[0])
    except:
        pass
    return 100

def _current_price(code: str) -> float:
    try:
        with OpenQuoteContext(host=HOST, port=PORT) as qctx:
            ret, data = qctx.get_market_snapshot([code])
            if ret == RET_OK and not data.empty:
                return float(data['last_price'].iloc[0])
    except:
        pass
    return 0.0


# ── Signal handler ────────────────────────────────────────────

def on_signal(sig: dict):
    if not _day_trade_enabled:
        return
    if _intraday_paused:
        print(f"[day_trader] 日內連輸熔斷，暫停交易")
        return

    # Check weekly limits first
    can, reason = _weekly.can_trade()
    if not can:
        print(f"[day_trader] Weekly block: {reason}")
        return

    code      = sig["code"]
    direction = sig["direction"]
    price     = sig["price"]
    sig_type  = sig["type"]
    vol_ratio = sig.get("vol_ratio", 1.0)

    # Ignore signals during startup warmup (historical candle replay)
    if time.time() - _start_time < STARTUP_WARMUP_SECS:
        return

    # TA conviction gate — candle signals need ≥3 (pattern is primary conviction);
    # all other signal types need ≥5
    ta_score_val   = sig.get("ta_score", 0)
    is_candle_sig  = sig_type in ("CANDLE_PATTERN_SIGNAL", "CANDLE_PATTERN_SHORT")
    ta_min         = 3 if is_candle_sig else 5
    if abs(ta_score_val) < ta_min:
        print(f"[day_trader] TA 評分 {ta_score_val}/10 不足（需 ≥{ta_min}），跳過 {code}")
        return

    # ATR regime filter — skip only if truly dead (no movement at all)
    # HK blue chips have naturally low ATR — use 0.15% threshold
    # US stocks: 0.1% threshold (prices are high, ATR in % terms is naturally small)
    try:
        from market_watcher import get_ta_results
        _atr_regime   = get_ta_results().get(code, {}).get("indicators", {}).get("atr")
        _atr_min_pct  = 0.001 if code.startswith("US.") else 0.0015
        if _atr_regime and price > 0 and _atr_regime < price * _atr_min_pct:
            print(f"[day_trader] {code} ATR={_atr_regime:.3f} 低於 {_atr_min_pct*100:.2f}% 市場死水，跳過")
            return
    except:
        pass

    # Block new entries in last 45 min before market close + opening 5-min blackout
    try:
        import pytz
        is_us = code.startswith("US.")
        if is_us:
            et  = pytz.timezone("America/New_York")
            now_local = datetime.now(et)
            t_min = now_local.hour * 60 + now_local.minute
            # Opening blackout: 09:30–09:35 ET
            if 9 * 60 + 30 <= t_min < 9 * 60 + 35:
                print(f"[day_trader] US 開市前5分鐘禁止新入場（高噪音期），跳過 {code}")
                return
            # US close 16:00 ET — block after 15:15
            if (15 * 60 + (60 - NO_NEW_ENTRY_MINS)) <= t_min < 16 * 60:
                print(f"[day_trader] US 收市前 {NO_NEW_ENTRY_MINS} 分鐘禁止新入場，跳過 {code}")
                return
        else:
            hkt = pytz.timezone('Asia/Hong_Kong')
            now_hkt = datetime.now(hkt)
            t_min = now_hkt.hour * 60 + now_hkt.minute
            # Opening blackout: 09:30–09:35 HKT
            if 9 * 60 + 30 <= t_min < 9 * 60 + 35:
                print(f"[day_trader] HK 開市前5分鐘禁止新入場（高噪音期），跳過 {code}")
                return
            if (11 * 60 + (60 - NO_NEW_ENTRY_MINS)) <= t_min < 12 * 60:
                print(f"[day_trader] 午市前 {NO_NEW_ENTRY_MINS} 分鐘禁止新入場，跳過 {code}")
                return
            if (15 * 60 + (60 - NO_NEW_ENTRY_MINS)) <= t_min < 16 * 60:
                print(f"[day_trader] HK 收市前 {NO_NEW_ENTRY_MINS} 分鐘禁止新入場，跳過 {code}")
                return
    except:
        pass

    # Gap day filter — skip stocks with > 2% gap from prior close (unpredictable fill direction)
    try:
        import yfinance as yf
        yf_sym = code.replace("HK.", "") + ".HK" if code.startswith("HK.") else code.replace("US.", "")
        hist = yf.Ticker(yf_sym).history(period="2d")
        if len(hist) >= 2:
            prior_close = float(hist["Close"].iloc[-2])
            open_price  = float(hist["Open"].iloc[-1])
            if prior_close > 0:
                gap_pct = abs(open_price - prior_close) / prior_close * 100
                if gap_pct > 2.0:
                    print(f"[day_trader] {code} 今日缺口 {gap_pct:.1f}% > 2%，方向不確定，跳過")
                    return
    except:
        pass

    # Re-entry cooldown after stop-loss
    cooldown_expiry = _cooldown.get(code, 0)
    if time.time() < cooldown_expiry:
        remaining = int((cooldown_expiry - time.time()) / 60)
        print(f"[day_trader] {code} 止損冷靜期，剩餘 {remaining} 分鐘")
        return

    # Disabled signal check (win rate < 35% after ≥10 trades)
    if sig_type in _get_disabled_signals():
        print(f"[day_trader] {sig_type} 勝率過低已停用，跳過 {code}")
        return

    is_hk_stock = code.startswith("HK.")

    # HSI trend filter — HK stocks only; US has its own market dynamics
    if is_hk_stock:
        hsi_chg = _hsi_change_pct()
        if direction == "BUY" and hsi_chg < -0.3:
            print(f"[day_trader] HSI {hsi_chg:.1f}%, blocking BUY {code}")
            return
        if direction == "SELL" and hsi_chg > 0.3:
            print(f"[day_trader] HSI {hsi_chg:.1f}%, blocking SHORT {code}")
            return

    # Southbound flow bias — HK stocks only; irrelevant for US equities
    if is_hk_stock:
        if _southbound_flow_hkd > SOUTHBOUND_THRESHOLD and direction == "SELL":
            ta_score_val = sig.get("ta_score", 0)
            if abs(ta_score_val) < 7:
                print(f"[day_trader] 南向大幅淨流入 {_southbound_flow_hkd:+.0f}M，需 TA≥7 才能做空 {code}")
                return
        if _southbound_flow_hkd < -SOUTHBOUND_THRESHOLD and direction == "BUY":
            ta_score_val = sig.get("ta_score", 0)
            if abs(ta_score_val) < 7:
                print(f"[day_trader] 南向大幅淨流出 {_southbound_flow_hkd:+.0f}M，需 TA≥7 才能做多 {code}")
                return

    # Risk/reward ratio filter — minimum 1:1.5
    risk   = abs(price - sig["stop"])
    reward = abs(sig["target"] - price)
    if risk == 0 or reward / risk < 1.5:
        print(f"[day_trader] R/R {reward/risk:.1f}:1 too low for {code}, skip")
        return

    with _lock:
        if len(_open_intraday) >= MAX_INTRADAY_POSITIONS:
            print(f"[day_trader] Max positions reached, ignoring {code}")
            return
        if any(p["code"] == code for p in _open_intraday):
            return
        sector = _SECTOR_MAP.get(code, "其他")
        sector_count = sum(1 for p in _open_intraday if _SECTOR_MAP.get(p["code"], "其他") == sector)
        if sector_count >= MAX_SAME_SECTOR:
            print(f"[day_trader] {sector} 板塊已有 {sector_count} 個倉，跳過 {code}")
            return

    # Safety check gate (claude-execute VWAP + RSI(3) + EMA(8) strategy)
    rules = json.load(open(os.path.join(os.path.dirname(__file__), "rules.json")))
    if not rules.get("paper_trading", True) or True:  # always run safety check
        try:
            from safety_check import run_safety_check
            sc = run_safety_check(
                code=code,
                price=price,
                ema8=sig.get("ema8", price),
                vwap=sig.get("vwap", price),
                rsi3=sig.get("rsi3", 50.0),
                rsi14=sig.get("rsi14", 50.0),
                paper_trading=rules.get("paper_trading", True),
                trade_size_hkd=PER_TRADE_BUDGET_HKD,
            )
            if not sc["all_pass"]:
                failed = [c["label"] for c in sc["conditions"] if not c["pass"]]
                print(f"[day_trader] Safety check BLOCKED {code}: {', '.join(failed)}")
                return
        except Exception as e:
            print(f"[day_trader] Safety check error (allowing through): {e}")

    # AI veto check
    if not _ai_should_trade(sig):
        print(f"[day_trader] AI vetoed {code} {sig_type}")
        return

    # ATR-based position sizing (risk 1% of pool per trade, capped by budget)
    lot_size = _get_lot_size(code)
    # US stocks: convert HKD budget to USD; lot_size is always 1 for US
    is_us_for_sizing = code.startswith("US.")
    if is_us_for_sizing:
        lot_size = 1  # US stocks trade in single shares on Futu
    budget_local = PER_TRADE_BUDGET_HKD / USD_HKD if is_us_for_sizing else PER_TRADE_BUDGET_HKD
    try:
        from market_watcher import get_ta_results
        atr = get_ta_results().get(code, {}).get("indicators", {}).get("atr")
    except:
        atr = None

    if atr and atr > 0:
        risk_budget_local = (DAY_TRADE_POOL_HKD * 0.01) / (USD_HKD if is_us_for_sizing else 1)
        qty_atr     = int(risk_budget_local / atr // lot_size) * lot_size
        qty_cap     = int(budget_local / price // lot_size) * lot_size
        qty         = min(qty_atr, qty_cap) if qty_atr > 0 else qty_cap
    else:
        qty = int(budget_local / price // lot_size) * lot_size

    # Score-proportional sizing: high conviction trades get larger allocation
    ta_score = sig.get("ta_score", 0)
    if abs(ta_score) >= 8:
        qty = int(qty * 1.3 // lot_size) * lot_size   # +30% for high-conviction
    elif abs(ta_score) < 6:
        qty = int(qty * 0.8 // lot_size) * lot_size   # -20% for low-conviction
    qty = min(qty, int(budget_local * 1.3 / price // lot_size) * lot_size)  # hard cap

    if qty == 0:
        print(f"[day_trader] Qty=0 for {code}, skip")
        return

    is_long = direction == "BUY"
    if not is_long and not _is_shortable(code):
        print(f"[day_trader] {code} 不在可沽空名單，跳過做空信號")
        return

    limit_price = round(price * (1.002 if is_long else 0.998), 3)
    # Simulate account only accepts BUY/SELL (not SELL_SHORT/BUY_BACK)
    trd_side    = TrdSide.BUY if is_long else TrdSide.SELL

    is_us_stock = code.startswith("US.")
    trd_market  = TrdMarket.US if is_us_stock else TrdMarket.HK

    try:
        with OpenSecTradeContext(filter_trdmarket=trd_market, host=HOST, port=PORT) as ctx:
            acc_id = _get_sim_acc_id(ctx)
            if not acc_id:
                return

            # Cap qty to available cash (leave 5% buffer for fees)
            if is_long:
                avl_cash_raw = _get_available_cash(ctx, acc_id) * 0.95
                # US cash is in USD — convert budget to USD for comparison
                budget_local = PER_TRADE_BUDGET_HKD / USD_HKD if is_us_stock else PER_TRADE_BUDGET_HKD
                min_lot_cost = limit_price * lot_size
                if avl_cash_raw < min_lot_cost:
                    currency = "USD" if is_us_stock else "HKD"
                    print(f"[day_trader] 可用資金 {currency} {avl_cash_raw:.0f} 不足購買 1 手 {code}，跳過")
                    return
                qty_cash = int(min(avl_cash_raw, budget_local) / limit_price // lot_size) * lot_size
                qty = min(qty, qty_cash)

            ret, data = ctx.place_order(
                price=limit_price, qty=qty, code=code,
                trd_side=trd_side, order_type=OrderType.NORMAL,
                trd_env=TrdEnv.SIMULATE, acc_id=acc_id,
                remark=f"daytrade_{sig_type}",
            )
            if ret != RET_OK:
                print(f"[day_trader] Order failed: {data}")
                return
            order_id = data['order_id'].iloc[0]
    except Exception as e:
        print(f"[day_trader] Place order exception: {e}")
        return

    # Journal entry
    action = "BUY" if is_long else "SELL_SHORT"
    from trade_journal import record_entry
    from price_data import get_price_data
    pd_data = get_price_data(code.replace("HK.", "").replace("US.", ""))
    journal_id = record_entry(
        ticker=code, action=action, qty=qty,
        entry_price=limit_price, stop=sig["stop"], target=sig["target"],
        signal_type=sig_type, reason=sig.get("reason", ""),
        rsi=pd_data.get("rsi"), vol_ratio=vol_ratio,
        ta_score=sig.get("ta_score", 0),
        candle_patterns=sig.get("candle_patterns", []),
        chart_patterns=sig.get("chart_patterns", []),
    )

    with _lock:
        _open_intraday.append({
            "journal_id":      journal_id,
            "code":            code,
            "direction":       direction,
            "qty":             qty,
            "lot_size":        lot_size,
            "entry":           limit_price,
            "stop":            sig["stop"],
            "target":          sig["target"],
            "highest":         limit_price,
            "lowest":          limit_price,
            "partial_taken":   False,
            "order_id":        order_id,
            "ta_score":        sig.get("ta_score", 0),
            "candle_patterns": sig.get("candle_patterns", []),
            "chart_patterns":  sig.get("chart_patterns", []),
            "entry_atr":       atr or 0.0,
            "entry_time":      time.time(),
        })

    action_tag = "BUY LONG" if is_long else "SELL SHORT"
    dir_emoji  = "🟢" if is_long else "🔴"
    print(f"[day_trader] ✅ {sig_type} {code} {action_tag} x{qty} @ {limit_price} | stop={sig['stop']} target={sig['target']}")
    _notify(
        f"{dir_emoji} <b>{action_tag} — {code}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Signal : {sig_type}\n"
        f"Price  : <b>${limit_price}</b>  x{qty} shares\n"
        f"Stop   : ${sig['stop']}\n"
        f"Target : ${sig['target']}\n"
        f"R/R    : 1:{reward/risk:.1f}   TA: {sig.get('ta_score', 'N/A')}/10\n"
        f"Reason : {sig.get('reason', '')}"
    )


# ── AI veto ───────────────────────────────────────────────────

def _ai_should_trade(sig: dict) -> bool:
    """Pre-trade AI veto. Returns False (block) if AI rejects, times out, or errors."""
    from trade_journal import get_recent_lessons
    lessons = get_recent_lessons(3)
    direction  = sig.get("direction", "BUY")
    risk_pct   = round(abs(sig['price'] - sig['stop'])   / (sig['price'] or 1) * 100, 1)
    reward_pct = round(abs(sig['target'] - sig['price']) / (sig['price'] or 1) * 100, 1)
    # Compact prompt — fewer tokens = faster response = fewer Groq timeouts
    prompt = (
        f"交易信號評估（只回JSON）：\n"
        f"信號={sig['type']} 方向={'做多' if direction == 'BUY' else '做空'} "
        f"股票={sig['code']} TA={sig.get('ta_score','N/A')}/10 "
        f"風險={risk_pct}% 回報={reward_pct}%\n"
        f"近期教訓：{lessons}\n"
        f"回覆格式：{{\"trade\": true/false, \"reason\": \"一句話\"}}"
    )

    # Try Groq first, fall back to Anthropic/Claude; default SKIP on any failure
    raw = None
    try:
        from ai_analyst import _ask
        raw = _ask(prompt, timeout=8)
    except Exception as e:
        print(f"[day_trader] Groq veto failed: {e}, trying Claude fallback")

    if raw is None:
        try:
            import anthropic, os
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
        except Exception as e:
            print(f"[day_trader] Claude veto also failed: {e} — defaulting to SKIP")
            return False  # fail-safe: no AI confirmation = no trade

    try:
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        start, end = raw.find("{"), raw.rfind("}") + 1
        result = json.loads(raw[start:end])
        decision = result.get("trade", False)  # default False if key missing
        print(f"[day_trader] AI veto: {'ALLOW' if decision else 'SKIP'} — {result.get('reason', '')}")
        return bool(decision)
    except Exception as e:
        print(f"[day_trader] AI veto parse failed: {e} — defaulting to SKIP")
        return False  # fail-safe: unparseable response = no trade


# ── Position monitor ──────────────────────────────────────────

def _monitor_positions():
    while True:
        time.sleep(10)
        with _lock:
            to_close = []
            for pos in list(_open_intraday):
                price   = _current_price(pos["code"])
                is_long = pos.get("direction", "BUY") == "BUY"
                if price <= 0:
                    continue

                # Minimum hold: skip all stop checks for first MIN_HOLD_SECS (avoids entry-candle noise)
                elapsed = time.time() - pos.get("entry_time", time.time())
                if elapsed < MIN_HOLD_SECS:
                    # Only allow catastrophic hard stop (> 5% against) during hold period
                    move_against = ((pos["entry"] - price) / pos["entry"]) if is_long else ((price - pos["entry"]) / pos["entry"])
                    if move_against < 0.05:
                        continue

                # ATR-dynamic trailing stop: wide initially (3×), tightens after MIN_HOLD_SECS (2×)
                entry_atr = pos.get("entry_atr", 0.0)
                if entry_atr and entry_atr > 0:
                    mult = TRAIL_ATR_MULT_TIGHT if elapsed >= MIN_HOLD_SECS else TRAIL_ATR_MULT
                    trail_dist = mult * entry_atr
                else:
                    trail_dist = pos["entry"] * TRAIL_PCT

                # Time stop: exit flat positions after TIME_STOP_MINUTES
                elapsed_min = (time.time() - pos.get("entry_time", time.time())) / 60
                if elapsed_min >= TIME_STOP_MINUTES and not pos["partial_taken"]:
                    move_pct = ((price - pos["entry"]) / pos["entry"]) * (1 if is_long else -1)
                    if move_pct < TIME_STOP_MIN_MOVE_PCT:
                        _close_position(pos, price, f"時間止損（{int(elapsed_min)}分鐘無進展）")
                        to_close.append(pos)
                        continue

                if is_long:
                    if price > pos.get("highest", pos["entry"]):
                        pos["highest"] = price
                    trail_stop     = round(pos["highest"] - trail_dist, 3)
                    effective_stop = max(pos["stop"], trail_stop)
                    if price <= effective_stop:
                        reason = (f"追蹤止損 ${price:.2f}（高位 ${pos['highest']:.2f}）"
                                  if trail_stop > pos["stop"] else f"止損觸發 ${price:.2f}")
                        _close_position(pos, price, reason)
                        to_close.append(pos)
                    elif price >= pos["target"]:
                        if not pos["partial_taken"]:
                            _partial_close(pos, price)
                        else:
                            _close_position(pos, price, f"最終目標 ${price:.2f}")
                            to_close.append(pos)
                else:  # short
                    if price < pos.get("lowest", pos["entry"]):
                        pos["lowest"] = price
                    trail_stop     = round(pos["lowest"] + trail_dist, 3)
                    effective_stop = min(pos["stop"], trail_stop)
                    if price >= effective_stop:
                        reason = (f"追蹤止損 ${price:.2f}（低位 ${pos['lowest']:.2f}）"
                                  if trail_stop < pos["stop"] else f"止損觸發 ${price:.2f}")
                        _close_position(pos, price, reason)
                        to_close.append(pos)
                    elif price <= pos["target"]:
                        if not pos["partial_taken"]:
                            _partial_close(pos, price)
                        else:
                            _close_position(pos, price, f"最終目標 ${price:.2f}")
                            to_close.append(pos)

            for pos in to_close:
                if pos in _open_intraday:
                    _open_intraday.remove(pos)


def _partial_close(pos: dict, price: float):
    """Close 50% at target, move stop to break-even, let rest run."""
    is_long   = pos.get("direction", "BUY") == "BUY"
    lot_size  = pos.get("lot_size", 100)
    half_qty  = (pos["qty"] // 2 // lot_size) * lot_size
    if half_qty <= 0:
        _close_position(pos, price, f"目標達到 ${price:.2f}")
        return

    # Simulate account: use BUY to close short, SELL to close long
    close_side = TrdSide.SELL if is_long else TrdSide.BUY
    close_px   = round(price * (0.998 if is_long else 1.002), 3)
    _close_mkt = TrdMarket.US if pos["code"].startswith("US.") else TrdMarket.HK
    try:
        with OpenSecTradeContext(filter_trdmarket=_close_mkt, host=HOST, port=PORT) as ctx:
            acc_id = _get_sim_acc_id(ctx)
            if acc_id:
                ctx.place_order(price=close_px, qty=half_qty, code=pos["code"],
                                trd_side=close_side, order_type=OrderType.NORMAL,
                                trd_env=TrdEnv.SIMULATE, acc_id=acc_id,
                                remark="daytrade_partial")
    except Exception as e:
        print(f"[day_trader] Partial close exception: {e}")

    pnl_hkd = round(((price - pos["entry"]) if is_long else (pos["entry"] - price)) * half_qty, 2)
    _weekly.add_pnl(pnl_hkd)

    pos["qty"]           -= half_qty
    pos["stop"]           = pos["entry"]   # move stop to break-even
    pos["partial_taken"]  = True

    pnl_pct = round(pnl_hkd / (pos["entry"] * half_qty or 1) * 100, 2)
    side_tag = "LONG" if is_long else "SHORT"
    print(f"[day_trader] 部分平倉 {pos['code']} x{half_qty} @ {price:.2f} | PnL HKD {pnl_hkd:+,.0f} | 剩餘 x{pos['qty']} 止損移至成本")
    _notify(
        f"📤 <b>PARTIAL CLOSE 50% — {pos['code']} {side_tag}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Closed : x{half_qty} @ ${price:.2f}\n"
        f"P&L    : <b>{'+' if pnl_pct > 0 else ''}{pnl_pct}%</b>  (HKD {pnl_hkd:+,.0f})\n"
        f"Remaining x{pos['qty']} — stop moved to entry ${pos['entry']}"
    )


def _close_position(pos: dict, exit_price: float, reason: str):
    is_long    = pos.get("direction", "BUY") == "BUY"
    is_us      = pos["code"].startswith("US.")
    # Simulate account: use BUY to close short, SELL to close long
    close_side = TrdSide.SELL if is_long else TrdSide.BUY
    close_px   = round(exit_price * (0.998 if is_long else 1.002), 3)
    close_mkt  = TrdMarket.US if is_us else TrdMarket.HK

    try:
        with OpenSecTradeContext(filter_trdmarket=close_mkt, host=HOST, port=PORT) as ctx:
            acc_id = _get_sim_acc_id(ctx)
            if not acc_id:
                return
            ret, data = ctx.place_order(
                price=close_px, qty=pos["qty"],
                code=pos["code"], trd_side=close_side,
                order_type=OrderType.NORMAL, trd_env=TrdEnv.SIMULATE,
                acc_id=acc_id, remark="daytrade_exit",
            )
            if ret != RET_OK:
                print(f"[day_trader] Close failed: {data}")
    except Exception as e:
        print(f"[day_trader] Close exception: {e}")

    # Update journal
    from trade_journal import record_exit
    record_exit(
        pos["journal_id"], exit_price, reason,
        candle_patterns=pos.get("candle_patterns", []),
        chart_patterns=pos.get("chart_patterns", []),
        ta_score=pos.get("ta_score", 0),
    )

    # Update weekly P&L — US positions are USD, convert to HKD for weekly tracker
    fx = USD_HKD if is_us else 1.0
    if is_long:
        pnl_local = round((exit_price - pos["entry"]) * pos["qty"], 2)
        pnl_pct   = round((exit_price - pos["entry"]) / (pos["entry"] or 1) * 100, 2)
    else:
        pnl_local = round((pos["entry"] - exit_price) * pos["qty"], 2)
        pnl_pct   = round((pos["entry"] - exit_price) / (pos["entry"] or 1) * 100, 2)
    pnl_hkd = round(pnl_local * fx, 2)
    _weekly.add_pnl(pnl_hkd)

    # Record pattern outcome for learning
    from technical_analysis import update_pattern_stats
    update_pattern_stats(
        signal_type=pos.get("type", "UNKNOWN"),
        candle_patterns=pos.get("candle_patterns", []),
        chart_patterns=pos.get("chart_patterns", []),
        ta_score=pos.get("ta_score", 0),
        outcome="WIN" if pnl_pct > 0 else "LOSS",
        pnl_pct=pnl_pct,
    )

    # Set re-entry cooldown if stop-loss triggered
    if "止損" in reason:
        _cooldown[pos["code"]] = time.time() + COOLDOWN_SECS
        print(f"[day_trader] {pos['code']} 止損冷靜期啟動，20 分鐘後可再入")

    global _consecutive_losses, _intraday_paused
    if pnl_hkd < 0:
        _consecutive_losses += 1
        if _consecutive_losses >= 3 and not _intraday_paused:
            _intraday_paused = True
            _notify(
                f"⚠️ <b>日內連輸熔斷！</b>\n"
                f"已連續虧損 {_consecutive_losses} 次，暫停今日日內交易。\n"
                f"明天 09:30 自動重置。"
            )
            print(f"[day_trader] 連輸 {_consecutive_losses} 次，觸發日內熔斷")
    else:
        _consecutive_losses = 0

    result_emoji = "🟢 WIN" if pnl_pct > 0 else "🔴 LOSS"
    side_tag     = "LONG" if is_long else "SHORT"
    weekly_state = _weekly.get_state()
    _notify(
        f"{result_emoji} — <b>CLOSED {side_tag} {pos['code']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Entry  : ${pos['entry']}  →  Exit : ${exit_price}\n"
        f"P&L    : <b>{'+' if pnl_pct > 0 else ''}{pnl_pct}%</b>  (HKD {pnl_hkd:+,.0f})\n"
        f"Reason : {reason}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"Week   : HKD {weekly_state['realized_pnl_hkd']:+,.0f}  ({weekly_state['pct_of_pool']:+.1f}%)"
    )
    print(f"[day_trader] 出場 {pos['code']} @ {exit_price}: {reason} | 本週累計 HKD {weekly_state['realized_pnl_hkd']:+.0f}")


def close_all_intraday(reason: str = "收市前平倉", market_filter: str = None):
    """Close all open intraday positions. market_filter='HK'|'US' closes only that market."""
    with _lock:
        remaining = []
        for pos in list(_open_intraday):
            code = pos["code"]
            if market_filter and not code.startswith(f"{market_filter}."):
                remaining.append(pos)
                continue
            price = _current_price(code)
            if price > 0:
                _close_position(pos, price, reason)
        _open_intraday.clear()
        _open_intraday.extend(remaining)


# ── Telegram notification ─────────────────────────────────────
# Use a queue + dedicated thread so asyncio.run() never conflicts
# with the bot's main event loop running on another thread.

import queue as _queue
_notify_queue: _queue.Queue = _queue.Queue()

def _notification_worker():
    import asyncio
    from dotenv import load_dotenv
    from telegram import Bot
    load_dotenv()
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    if not bot_token or not chat_id:
        return
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        text = _notify_queue.get()
        try:
            loop.run_until_complete(
                Bot(token=bot_token).send_message(
                    chat_id=chat_id, text=text, parse_mode="HTML"
                )
            )
        except Exception as e:
            print(f"[day_trader] Notify failed: {e}")

threading.Thread(target=_notification_worker, daemon=True, name="notify-worker").start()

def _notify(text: str):
    _notify_queue.put(text)


# ── Public API ────────────────────────────────────────────────

def set_southbound_bias(flow_hkd_millions: float):
    global _southbound_flow_hkd
    _southbound_flow_hkd = flow_hkd_millions
    bias = "看漲" if flow_hkd_millions > SOUTHBOUND_THRESHOLD else "看跌" if flow_hkd_millions < -SOUTHBOUND_THRESHOLD else "中性"
    print(f"[day_trader] 南向流量偏向：{bias} ({flow_hkd_millions:+.0f}M HKD)")

def reset_intraday_state():
    global _consecutive_losses, _intraday_paused, _day_trade_enabled, _cooldown
    _consecutive_losses = 0
    _intraday_paused    = False
    _day_trade_enabled  = True
    _cooldown           = {}
    print("[day_trader] 日內狀態已重置")

def set_enabled(enabled: bool):
    global _day_trade_enabled
    _day_trade_enabled = enabled
    print(f"[day_trader] {'enabled' if enabled else 'disabled'}")

def is_enabled() -> bool:
    return _day_trade_enabled

def get_open_positions() -> list:
    with _lock:
        return list(_open_intraday)

def get_weekly_state() -> dict:
    return _weekly.get_state()

def start_monitor():
    threading.Thread(target=_monitor_positions, daemon=True).start()
    print("[day_trader] Position monitor started")
