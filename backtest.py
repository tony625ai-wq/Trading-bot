import yfinance as yf
import pandas as pd

def run_backtest(ticker: str, months: int = 6) -> dict:
    """
    簡單回測：模擬過去 N 個月，每次突破 50日線買入，
    跌穿止損(-7%)或升+10%出場，計算勝率和平均回報
    """
    hist = yf.Ticker(ticker).history(period=f"{months * 30 + 50}d")
    if len(hist) < 60:
        return {"error": "歷史數據不足"}

    hist["MA50"] = hist["Close"].rolling(50).mean()
    hist = hist.dropna()

    trades = []
    in_trade = False
    entry_price = 0
    stop = 0
    target = 0

    for i in range(1, len(hist)):
        prev = hist.iloc[i - 1]
        curr = hist.iloc[i]

        if not in_trade:
            # 入場條件：收市價突破 50日線
            if prev["Close"] < prev["MA50"] and curr["Close"] > curr["MA50"]:
                entry_price = curr["Close"]
                stop = entry_price * 0.93
                target = entry_price * 1.10
                in_trade = True
        else:
            # 出場條件
            if curr["Low"] <= stop:
                pnl = round((stop - entry_price) / entry_price * 100, 2)
                trades.append({"result": "LOSS", "pnl": pnl, "date": str(curr.name.date())})
                in_trade = False
            elif curr["High"] >= target:
                pnl = round((target - entry_price) / entry_price * 100, 2)
                trades.append({"result": "WIN", "pnl": pnl, "date": str(curr.name.date())})
                in_trade = False

    if not trades:
        return {"ticker": ticker, "trades": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0}

    wins = [t for t in trades if t["result"] == "WIN"]
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    return {
        "ticker": ticker,
        "period_months": months,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(trades) - len(wins),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_pnl": round(total_pnl / len(trades), 2),
        "total_pnl": total_pnl,
        "trade_log": trades[-5:],  # 最近5筆
    }
