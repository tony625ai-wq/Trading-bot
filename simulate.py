"""
1-Year Candle-by-Candle Simulation
Feeds 1h historical data as if live, uses the real candle_patterns.py for signals,
records pattern outcomes to pattern_learning.json, and reports results via Telegram.

Usage:
    python simulate.py              # all tickers
    python simulate.py HK.00700     # single ticker
    python simulate.py --send       # send results to Telegram
"""
import sys
import json
import asyncio
import warnings
warnings.filterwarnings("ignore")

import yfinance as yf
import pandas as pd

import candle_patterns
from technical_analysis import update_pattern_stats

# ── Config ────────────────────────────────────────────────────────────────────
PERIOD           = "1y"
INTERVAL         = "1h"
MIN_CONFIDENCE   = 60
ATR_STOP_MULT    = 1.5
ATR_TARGET_MULT  = 3.0
RISK_PER_TRADE   = 0.02          # 2% of capital per trade
STARTING_CAPITAL = 500_000       # HKD
COMMISSION       = 60            # HKD round-trip
SLIPPAGE         = 0.001         # 0.1% each side

HK_TICKERS = {
    "HK.01024": "1024.HK",   # Kuaishou      -0.56%
    "HK.00175": "0175.HK",   # Geely Auto    -0.59%
    "HK.00669": "0669.HK",   # Techtronic    -0.83%
    "HK.06862": "6862.HK",   # Haidilao      -0.99%
    "HK.02318": "2318.HK",   # Ping An       -1.10%
    "HK.01810": "1810.HK",   # Xiaomi        -1.25%
    "HK.00941": "0941.HK",   # China Mobile  -1.28%
    "HK.00700": "0700.HK",   # Tencent       -1.31%
    "HK.00005": "0005.HK",   # HSBC          -1.33%
    "HK.09988": "9988.HK",   # Alibaba       -1.41%
}
US_TICKERS = {
    "US.PANW":  "PANW",   # Palo Alto     -0.11%
    "US.AMD":   "AMD",    # AMD           -0.13%
    "US.COIN":  "COIN",   # Coinbase      -0.20%
    "US.PYPL":  "PYPL",   # PayPal        -0.66%
    "US.CRWD":  "CRWD",   # CrowdStrike   -0.71%
    "US.AAPL":  "AAPL",   # Apple         -0.73%
    "US.GOOGL": "GOOGL",  # Google        -0.84%
    "US.NVDA":  "NVDA",   # Nvidia        -0.90%
    "US.DDOG":  "DDOG",   # Datadog       -0.93%
    "US.CRM":   "CRM",    # Salesforce    -1.12%
}
ALL_TICKERS = {**HK_TICKERS, **US_TICKERS}


# ── Data ──────────────────────────────────────────────────────────────────────

def _download(yf_sym: str) -> pd.DataFrame:
    df = yf.Ticker(yf_sym).history(period=PERIOD, interval=INTERVAL)
    if df.empty or len(df) < 50:
        return pd.DataFrame()
    df = df[["Open", "High", "Low", "Close", "Volume"]].rename(columns=str.lower)
    df = df.dropna()
    return df


def _indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    ema9   = c.ewm(span=9).mean()
    ema21  = c.ewm(span=21).mean()
    delta  = c.diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rsi    = 100 - (100 / (1 + gain / loss))
    vm20   = df["volume"].rolling(20).mean()
    rvol   = df["volume"] / vm20.replace(0, 1)
    ema12  = c.ewm(span=12).mean()
    ema26  = c.ewm(span=26).mean()
    macd   = ema12 - ema26
    ms     = macd.ewm(span=9).mean()
    h, l   = df["high"], df["low"]
    tr     = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr    = tr.rolling(14).mean()
    return df.assign(ema9=ema9, ema21=ema21, rsi=rsi, rvol=rvol,
                     macd=macd, macd_signal=ms, atr=atr)


# ── Simulation ────────────────────────────────────────────────────────────────

def _to_candle_list(df: pd.DataFrame, window: int) -> list:
    """Convert last `window` rows to the dict format candle_patterns expects."""
    return [{"o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume}
            for _, r in df.tail(window).iterrows()]


def run_simulation(code: str, yf_sym: str) -> dict:
    df_raw = _download(yf_sym)
    if df_raw.empty:
        return {"code": code, "error": "數據下載失敗"}

    df = _indicators(df_raw)
    capital   = float(STARTING_CAPITAL)
    trades    = []
    pos       = None   # open position dict or None

    for i in range(50, len(df)):
        window   = df.iloc[:i]
        row      = df.iloc[i]
        price    = float(row["close"])
        atr      = float(row["atr"]) if pd.notna(row["atr"]) and row["atr"] > 0 else price * 0.01

        # ── Exit check ────────────────────────────────────────────
        if pos:
            if pos["direction"] == "long":
                hit_stop   = price <= pos["stop"]
                hit_target = price >= pos["target"]
            else:
                hit_stop   = price >= pos["stop"]
                hit_target = price <= pos["target"]

            if hit_stop or hit_target:
                reason = "TARGET" if hit_target else "STOP"
                if pos["direction"] == "long":
                    pnl_pct = (price * (1 - SLIPPAGE) - pos["entry"]) / pos["entry"] * 100
                else:
                    pnl_pct = (pos["entry"] - price * (1 + SLIPPAGE)) / pos["entry"] * 100

                pnl_hkd = capital * RISK_PER_TRADE * pnl_pct / 100 - COMMISSION
                capital += pnl_hkd
                outcome = "WIN" if pnl_hkd > 0 else "LOSS"

                update_pattern_stats(
                    signal_type=f"CANDLE_{pos['direction'].upper()}",
                    candle_patterns=pos["patterns"],
                    chart_patterns=[],
                    ta_score=pos["confidence"] // 10,
                    outcome=outcome,
                    pnl_pct=round(pnl_pct, 3),
                )

                trades.append({
                    "date":      str(df.index[i].date()),
                    "direction": pos["direction"],
                    "entry":     round(pos["entry"], 4),
                    "exit":      round(price, 4),
                    "pnl_pct":   round(pnl_pct, 3),
                    "pnl_hkd":   round(pnl_hkd, 2),
                    "result":    outcome,
                    "reason":    reason,
                    "patterns":  [p["name"] for p in pos["patterns"]],
                })
                pos = None
            continue

        # ── Entry check ───────────────────────────────────────────
        if pd.isna(row["rsi"]) or pd.isna(row["ema9"]):
            continue

        candles_list   = _to_candle_list(window, 10)
        df_slice       = window.tail(10).copy()
        pats           = candle_patterns.detect(df_slice)
        if not pats:
            continue

        direction, confidence = candle_patterns.score_signal(
            pats,
            rsi=float(row["rsi"]),
            ema9=float(row["ema9"]),
            ema21=float(row["ema21"]),
            macd=float(row["macd"]) if pd.notna(row["macd"]) else 0,
            macd_signal=float(row["macd_signal"]) if pd.notna(row["macd_signal"]) else 0,
            rvol=float(row["rvol"]) if pd.notna(row["rvol"]) else 1,
        )

        if direction == "hold" or confidence < MIN_CONFIDENCE:
            continue

        entry = price * (1 + SLIPPAGE) if direction == "long" else price * (1 - SLIPPAGE)
        if direction == "long":
            stop   = round(entry - ATR_STOP_MULT * atr, 6)
            target = round(entry + ATR_TARGET_MULT * atr, 6)
        else:
            stop   = round(entry + ATR_STOP_MULT * atr, 6)
            target = round(entry - ATR_TARGET_MULT * atr, 6)

        pos = {"direction": direction, "entry": entry,
               "stop": stop, "target": target,
               "patterns": pats, "confidence": confidence}

    # ── Results ───────────────────────────────────────────────────
    if not trades:
        return {"code": code, "trades": 0, "message": "無觸發交易信號"}

    wins     = [t for t in trades if t["result"] == "WIN"]
    total_pnl = sum(t["pnl_hkd"] for t in trades)
    win_rate  = round(len(wins) / len(trades) * 100, 1)
    avg_pct   = round(sum(t["pnl_pct"] for t in trades) / len(trades), 3)
    max_loss  = round(min(t["pnl_pct"] for t in trades), 3)
    top_pats  = {}
    for t in trades:
        for p in t["patterns"]:
            top_pats.setdefault(p, {"trades": 0, "wins": 0})
            top_pats[p]["trades"] += 1
            if t["result"] == "WIN":
                top_pats[p]["wins"] += 1

    return {
        "code":          code,
        "data_bars":     len(df),
        "trades":        len(trades),
        "wins":          len(wins),
        "losses":        len(trades) - len(wins),
        "win_rate":      win_rate,
        "avg_pnl_pct":   avg_pct,
        "total_pnl_hkd": round(total_pnl, 0),
        "final_capital": round(capital, 0),
        "return_pct":    round((capital - STARTING_CAPITAL) / STARTING_CAPITAL * 100, 2),
        "max_loss_pct":  max_loss,
        "pattern_stats": {k: round(v["wins"]/v["trades"]*100, 1)
                          for k, v in top_pats.items() if v["trades"] >= 2},
        "recent_trades": trades[-3:],
    }


# ── Runner ────────────────────────────────────────────────────────────────────

def run_all(send_telegram: bool = False) -> list:
    results = []
    total_pnl = 0
    print(f"\n{'='*60}")
    print(f"  1年回模擬  |  {PERIOD} {INTERVAL}  |  {len(ALL_TICKERS)} 支股票")
    print(f"{'='*60}\n")

    for code, yf_sym in ALL_TICKERS.items():
        print(f"[{code}] 下載中...", end=" ", flush=True)
        r = run_simulation(code, yf_sym)
        results.append(r)
        if "error" in r or r.get("trades", 0) == 0:
            print(r.get("error") or r.get("message", "無信號"))
            continue
        total_pnl += r.get("total_pnl_hkd", 0)
        print(f"交易 {r['trades']} 筆 | 勝率 {r['win_rate']}% | "
              f"P&L HKD {r['total_pnl_hkd']:+,.0f} | 回報 {r['return_pct']:+.2f}%")
        for t in r.get("recent_trades", []):
            tag = "✅" if t["result"] == "WIN" else "❌"
            print(f"  {tag} {t['date']} {t['direction'].upper()} "
                  f"{t['entry']:.3f}→{t['exit']:.3f}  {t['pnl_pct']:+.2f}%  "
                  f"[{t['reason']}] pats: {t['patterns']}")

    print(f"\n{'='*60}")
    print(f"  全部合計 P&L: HKD {total_pnl:+,.0f}")
    print(f"{'='*60}\n")

    if send_telegram:
        asyncio.run(_send_report(results, total_pnl))

    return results


async def _send_report(results: list, total_pnl: float):
    from telegram_control import Bot, BOT_TOKEN, YOUR_CHAT_ID
    bot = Bot(token=BOT_TOKEN)

    lines = ["📊 <b>1年回溯模擬結果</b>\n"]
    for r in results:
        if "error" in r or r.get("trades", 0) == 0:
            lines.append(f"⚪ {r['code']}: {r.get('error') or '無信號'}")
            continue
        emoji = "🟢" if r["total_pnl_hkd"] > 0 else "🔴"
        lines.append(
            f"{emoji} <b>{r['code']}</b>  "
            f"{r['trades']}筆 | 勝率{r['win_rate']}% | "
            f"HKD{r['total_pnl_hkd']:+,.0f} ({r['return_pct']:+.1f}%)"
        )
        if r.get("pattern_stats"):
            best = max(r["pattern_stats"], key=r["pattern_stats"].get)
            lines.append(f"   🕯️ 最佳形態: {best} {r['pattern_stats'][best]}%勝率")

    lines.append(f"\n<b>合計: HKD {total_pnl:+,.0f}</b>")
    lines.append("pattern_learning.json 已更新 ✅")

    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await bot.send_message(chat_id=YOUR_CHAT_ID, text=text[i:i+3800], parse_mode="HTML")


if __name__ == "__main__":
    send = "--send" in sys.argv
    specific = [a for a in sys.argv[1:] if not a.startswith("--")]

    if specific:
        target = {k: v for k, v in ALL_TICKERS.items() if k in specific}
        if not target:
            print(f"找不到: {specific}，有效: {list(ALL_TICKERS.keys())}")
            sys.exit(1)
        for code, sym in target.items():
            r = run_simulation(code, sym)
            print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        run_all(send_telegram=send)
