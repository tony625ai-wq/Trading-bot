import json
import os
from datetime import datetime, timezone

TRADES_FILE = os.path.join(os.path.dirname(__file__), "trades.json")

def _load() -> list:
    if not os.path.exists(TRADES_FILE):
        return []
    with open(TRADES_FILE) as f:
        return json.load(f)

def _save(trades: list):
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

def record_trade(ticker: str, action: str, qty: int, entry_price: float,
                 stop_loss: float, target1: float, target2: float):
    trades = _load()
    trades.append({
        "id": len(trades) + 1,
        "ticker": ticker,
        "action": action,
        "qty": qty,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target1": target1,
        "target2": target2,
        "status": "OPEN",
        "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "closed_at": None,
        "exit_price": None,
        "pnl": None,
    })
    _save(trades)
    print(f"[tracker] 記錄交易：{action} {ticker} x{qty} @ ${entry_price}")

def get_open_trades() -> list:
    return [t for t in _load() if t["status"] == "OPEN"]

def close_trade(trade_id: int, exit_price: float):
    trades = _load()
    for t in trades:
        if t["id"] == trade_id and t["status"] == "OPEN":
            pnl_pct = ((exit_price - t["entry_price"]) / t["entry_price"]) * 100
            if t["action"] == "SELL":
                pnl_pct = -pnl_pct
            t["status"] = "CLOSED"
            t["exit_price"] = exit_price
            t["pnl"] = round(pnl_pct, 2)
            t["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _save(trades)

def daily_pnl_report() -> dict:
    """取得所有持倉的當前損益"""
    import yfinance as yf
    open_trades = get_open_trades()
    if not open_trades:
        return {"trades": [], "total_pnl": 0}

    results = []
    total_pnl = 0
    for t in open_trades:
        try:
            price = yf.Ticker(t["ticker"]).history(period="1d")["Close"].iloc[-1]
            pnl_pct = ((price - t["entry_price"]) / t["entry_price"]) * 100
            if t["action"] == "SELL":
                pnl_pct = -pnl_pct
            pnl_pct = round(pnl_pct, 2)
            hit_stop = price <= t["stop_loss"] if t["action"] == "BUY" else price >= t["stop_loss"]
            hit_t1 = price >= t["target1"] if t["action"] == "BUY" else price <= t["target1"]
            results.append({
                "ticker": t["ticker"],
                "entry": t["entry_price"],
                "current": round(float(price), 2),
                "stop_loss": t["stop_loss"],
                "target1": t["target1"],
                "pnl_pct": pnl_pct,
                "hit_stop": hit_stop,
                "hit_target1": hit_t1,
                "opened_at": t["opened_at"],
            })
            total_pnl += pnl_pct
        except Exception as e:
            print(f"[tracker] 取 {t['ticker']} 價失敗: {e}")

    return {"trades": results, "total_pnl": round(total_pnl, 2)}
