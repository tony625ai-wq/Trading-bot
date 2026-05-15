"""
Crypto paper trading engine — long & short, learns candle patterns.
State persisted in crypto_paper_trades.json + crypto_pattern_learning.json
"""
import json
import os
from datetime import datetime, timezone

import candle_patterns
import crypto_data

TRADES_FILE   = os.path.join(os.path.dirname(__file__), "crypto_paper_trades.json")
PATTERNS_FILE = os.path.join(os.path.dirname(__file__), "crypto_pattern_learning.json")

STARTING_CAPITAL = 10_000.0   # USDT
MAX_POSITIONS    = 3
RISK_PER_TRADE   = 0.05        # 5% of portfolio per trade
ATR_STOP_MULT    = 1.5         # stop = entry ± 1.5× ATR
ATR_TARGET_MULT  = 3.0         # target = entry ± 3.0× ATR (R:R 2:1)
MIN_CONFIDENCE   = 45          # minimum signal confidence to trade


# ── State helpers ─────────────────────────────────────────────────────────────

def _load_trades() -> dict:
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    return {"capital": STARTING_CAPITAL, "open": [], "closed": [], "stats": {
        "total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0
    }}


def _save_trades(state: dict):
    with open(TRADES_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _load_patterns() -> dict:
    if os.path.exists(PATTERNS_FILE):
        with open(PATTERNS_FILE) as f:
            return json.load(f)
    return {}


def _save_patterns(data: dict):
    with open(PATTERNS_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Core cycle ────────────────────────────────────────────────────────────────

def run_cycle() -> list[str]:
    """Run one 15-min cycle. Returns list of action messages."""
    msgs = []
    state = _load_trades()

    # 1. Check exits on open positions
    for pos in list(state["open"]):
        price = crypto_data.get_current_price(pos["symbol"])
        if price <= 0:
            continue
        hit_stop = hit_target = False
        if pos["direction"] == "long":
            hit_stop   = price <= pos["stop_loss"]
            hit_target = price >= pos["target"]
        else:
            hit_stop   = price >= pos["stop_loss"]
            hit_target = price <= pos["target"]

        if hit_stop or hit_target:
            reason = "TARGET ✅" if hit_target else "STOP ❌"
            pnl_pct = _close_position(state, pos, price, reason)
            msgs.append(f"{'🟢' if hit_target else '🔴'} {pos['symbol']} {pos['direction'].upper()} "
                        f"closed @ {price:.4f} | {reason} | P&L {pnl_pct:+.2f}%")
            _record_pattern_outcome(pos, hit_target)

    # 2. Look for new entries
    if len(state["open"]) < MAX_POSITIONS:
        for symbol in crypto_data.SYMBOLS:
            if any(p["symbol"] == symbol for p in state["open"]):
                continue
            msg = _evaluate_entry(state, symbol)
            if msg:
                msgs.append(msg)
            if len(state["open"]) >= MAX_POSITIONS:
                break

    _save_trades(state)
    return msgs


def _evaluate_entry(state: dict, symbol: str) -> str | None:
    df_15m = crypto_data.get_klines(symbol, "15m", 100)
    df_1h  = crypto_data.get_klines(symbol, "1h",  100)
    if df_15m.empty or len(df_15m) < 10:
        return None

    patterns_15m = candle_patterns.detect(df_15m)
    patterns_1h  = candle_patterns.detect(df_1h) if not df_1h.empty else []
    all_patterns = patterns_15m + patterns_1h

    row = df_15m.iloc[-1]
    direction, confidence = candle_patterns.score_signal(
        all_patterns,
        rsi=row["rsi"], ema9=row["ema9"], ema21=row["ema21"],
        macd=row["macd"], macd_signal=row["macd_signal"], rvol=row["rvol"]
    )

    if direction == "hold" or confidence < MIN_CONFIDENCE:
        return None

    price = float(row["close"])
    atr   = float(row["atr"]) if row["atr"] > 0 else price * 0.01

    if direction == "long":
        stop_loss = round(price - ATR_STOP_MULT * atr, 6)
        target    = round(price + ATR_TARGET_MULT * atr, 6)
    else:
        stop_loss = round(price + ATR_STOP_MULT * atr, 6)
        target    = round(price - ATR_TARGET_MULT * atr, 6)

    size_usdt = state["capital"] * RISK_PER_TRADE
    qty       = round(size_usdt / price, 6)

    pos = {
        "id":         len(state["closed"]) + len(state["open"]) + 1,
        "symbol":     symbol,
        "direction":  direction,
        "entry":      price,
        "stop_loss":  stop_loss,
        "target":     target,
        "qty":        qty,
        "size_usdt":  round(size_usdt, 2),
        "confidence": confidence,
        "patterns":   [p["name"] for p in all_patterns],
        "opened_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    state["open"].append(pos)

    pattern_names = ", ".join(p["name"] for p in all_patterns) or "none"
    return (f"{'📈' if direction == 'long' else '📉'} PAPER {direction.upper()} {symbol} "
            f"@ {price:.4f} | SL {stop_loss:.4f} | TP {target:.4f} | "
            f"conf {confidence}% | patterns: {pattern_names}")


def _close_position(state: dict, pos: dict, exit_price: float, reason: str) -> float:
    if pos["direction"] == "long":
        pnl_pct = (exit_price - pos["entry"]) / pos["entry"] * 100
    else:
        pnl_pct = (pos["entry"] - exit_price) / pos["entry"] * 100

    pnl_usdt = pos["size_usdt"] * pnl_pct / 100
    state["capital"] += pnl_usdt
    pos.update({
        "exit":      exit_price,
        "pnl_pct":   round(pnl_pct, 3),
        "pnl_usdt":  round(pnl_usdt, 2),
        "reason":    reason,
        "closed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })
    state["open"].remove(pos)
    state["closed"].append(pos)

    s = state["stats"]
    s["total_trades"] += 1
    s["wins"]   += 1 if pnl_pct > 0 else 0
    s["losses"] += 1 if pnl_pct <= 0 else 0
    s["total_pnl_pct"] = round(s["total_pnl_pct"] + pnl_pct, 3)

    return round(pnl_pct, 2)


# ── Pattern learning ──────────────────────────────────────────────────────────

def _record_pattern_outcome(pos: dict, was_win: bool):
    data = _load_patterns()
    for pname in pos.get("patterns", []):
        key = f"{pname}_{pos['direction']}"
        entry = data.setdefault(key, {
            "pattern": pname, "direction": pos["direction"],
            "trades": 0, "wins": 0, "win_rate": 0.0,
            "last_seen": ""
        })
        entry["trades"] += 1
        entry["wins"]   += 1 if was_win else 0
        entry["win_rate"] = round(entry["wins"] / entry["trades"] * 100, 1)
        entry["last_seen"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _save_patterns(data)


# ── Summary helpers ───────────────────────────────────────────────────────────

def get_summary() -> dict:
    state = _load_trades()
    s = state["stats"]
    win_rate = round(s["wins"] / s["total_trades"] * 100, 1) if s["total_trades"] else 0
    return {
        "capital":      round(state["capital"], 2),
        "pnl_usdt":     round(state["capital"] - STARTING_CAPITAL, 2),
        "pnl_pct":      round((state["capital"] - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2),
        "total_trades": s["total_trades"],
        "win_rate":     win_rate,
        "open_positions": len(state["open"]),
        "open":         state["open"],
    }


def get_top_patterns(n: int = 5) -> list[dict]:
    data = _load_patterns()
    qualified = [v for v in data.values() if v["trades"] >= 3]
    return sorted(qualified, key=lambda x: x["win_rate"], reverse=True)[:n]
