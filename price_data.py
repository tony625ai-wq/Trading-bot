import yfinance as yf

def get_price_data(ticker: str) -> dict:
    try:
        hist = yf.Ticker(ticker).history(period="200d")
        if hist.empty or len(hist) < 20:
            return {}

        close = hist["Close"]
        current = round(float(close.iloc[-1]), 2)
        ma50  = round(float(close.tail(50).mean()), 2) if len(close) >= 50 else None
        ma200 = round(float(close.mean()), 2)

        # RSI (14)
        delta = close.diff()
        gain = delta.clip(lower=0).tail(14).mean()
        loss = (-delta.clip(upper=0)).tail(14).mean()
        rsi = round(100 - (100 / (1 + gain / loss)), 1) if loss != 0 else 100

        # MACD (12,26,9)
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9).mean()
        macd_val = round(float(macd_line.iloc[-1]), 3)
        signal_val = round(float(signal_line.iloc[-1]), 3)
        macd_bullish = macd_val > signal_val

        # 布林帶 (20日)
        ma20 = close.tail(20).mean()
        std20 = close.tail(20).std()
        bb_upper = round(float(ma20 + 2 * std20), 2)
        bb_lower = round(float(ma20 - 2 * std20), 2)

        week52_high = round(float(hist["High"].max()), 2)
        week52_low  = round(float(hist["Low"].min()), 2)
        stop  = round(max(ma50 or current * 0.93, current * 0.93), 2)
        target1 = round(current * 1.10, 2)
        target2 = round(max(current * 1.20, week52_high), 2)

        # 技術評分（滿分5）
        score = sum([
            current > (ma50 or 0),    # 價高於MA50
            current > ma200,           # 價高於MA200
            rsi < 70,                  # RSI 未過熱
            rsi > 40,                  # RSI 未過冷
            macd_bullish,              # MACD 金叉
        ])

        return {
            "ticker": ticker,
            "current_price": current,
            "ma50": ma50,
            "ma200": ma200,
            "rsi": rsi,
            "macd": macd_val,
            "macd_signal": signal_val,
            "macd_bullish": macd_bullish,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "week52_high": week52_high,
            "week52_low": week52_low,
            "stop_loss": stop,
            "target1": target1,
            "target2": target2,
            "above_ma50": current > (ma50 or 0),
            "above_ma200": current > ma200,
            "tech_score": f"{score}/5",
        }
    except Exception as e:
        print(f"[price_data] {ticker} 取價失敗: {e}")
        return {}
