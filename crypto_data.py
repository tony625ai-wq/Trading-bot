import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone

BINANCE_BASE = "https://api.binance.com/api/v3"

SYMBOLS = {
    "BTCUSDT":  "BTC-USD",
    "ETHUSDT":  "ETH-USD",
    "SOLUSDT":  "SOL-USD",
    "BNBUSDT":  "BNB-USD",
    "XRPUSDT":  "XRP-USD",
    "ADAUSDT":  "ADA-USD",
}

INTERVAL_MAP = {"15m": "15m", "1h": "60m"}


def get_binance_klines(symbol: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame:
    try:
        r = requests.get(f"{BINANCE_BASE}/klines",
                         params={"symbol": symbol, "interval": interval, "limit": limit},
                         timeout=10)
        r.raise_for_status()
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"
        ])
        df = df.assign(**{col: df[col].astype(float) for col in ["open", "high", "low", "close", "volume"]})
        df = df.assign(time=pd.to_datetime(df["open_time"], unit="ms", utc=True))
        df = df[["time", "open", "high", "low", "close", "volume"]].set_index("time")
        return _add_indicators(df)
    except Exception as e:
        print(f"[crypto_data] Binance {symbol} {interval} 失敗: {e}")
        return pd.DataFrame()


def get_yf_klines(symbol: str, interval: str = "15m", period: str = "5d") -> pd.DataFrame:
    yf_sym = SYMBOLS.get(symbol, symbol)
    yf_interval = INTERVAL_MAP.get(interval, interval)
    try:
        df = yf.Ticker(yf_sym).history(period=period, interval=yf_interval)
        if df.empty:
            return pd.DataFrame()
        df = df[["Open", "High", "Low", "Close", "Volume"]].rename(columns=str.lower)
        df.index = df.index.tz_convert("UTC")
        return _add_indicators(df)
    except Exception as e:
        print(f"[crypto_data] yfinance {symbol} {interval} 失敗: {e}")
        return pd.DataFrame()


def get_klines(symbol: str, interval: str = "15m", limit: int = 200) -> pd.DataFrame:
    df = get_binance_klines(symbol, interval, limit)
    if df.empty:
        df = get_yf_klines(symbol, interval)
    return df


def get_current_price(symbol: str) -> float:
    try:
        r = requests.get(f"{BINANCE_BASE}/ticker/price",
                         params={"symbol": symbol}, timeout=5)
        return float(r.json()["price"])
    except Exception:
        df = get_binance_klines(symbol, "1m", 1)
        return float(df["close"].iloc[-1]) if not df.empty else 0.0


def _add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["close"]
    df["ema9"]  = c.ewm(span=9).mean()
    df["ema21"] = c.ewm(span=21).mean()
    df["ema50"] = c.ewm(span=50).mean()

    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    df["rsi"] = 100 - (100 / (1 + gain / loss))

    vol_ma20 = df["volume"].rolling(20).mean()
    df["vol_ma20"] = vol_ma20
    df["rvol"]     = df["volume"] / vol_ma20.replace(0, 1)

    ema12 = c.ewm(span=12).mean()
    ema26 = c.ewm(span=26).mean()
    macd  = ema12 - ema26
    df["macd"]        = macd
    df["macd_signal"] = macd.ewm(span=9).mean()

    df["atr"] = _atr(df)
    return df


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()
