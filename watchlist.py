import json
import os

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
DEFAULT = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "JPM", "V", "BRK-B"]

def load() -> list:
    if not os.path.exists(WATCHLIST_FILE):
        save(DEFAULT)
        return DEFAULT
    with open(WATCHLIST_FILE) as f:
        return json.load(f)

def save(tickers: list):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump([t.upper() for t in tickers], f)

def add(ticker: str) -> bool:
    tickers = load()
    if ticker.upper() in tickers:
        return False
    tickers.append(ticker.upper())
    save(tickers)
    return True

def remove(ticker: str) -> bool:
    tickers = load()
    if ticker.upper() not in tickers:
        return False
    tickers.remove(ticker.upper())
    save(tickers)
    return True
