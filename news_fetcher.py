import requests
import feedparser
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

NEWS_SOURCES = [
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("CNBC",          "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147"),
    ("BBC Business",  "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("MarketWatch",   "https://feeds.marketwatch.com/marketwatch/topstories/"),
]

TIMEOUT = 8

def _parse_time(raw: str) -> str:
    try:
        dt = parsedate_to_datetime(raw).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except:
        return raw[:16] if raw else "—"

def fetch_daily_news() -> list[dict]:
    news = []
    for source_name, url in NEWS_SOURCES:
        try:
            response = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:8]:
                news.append({
                    "title": entry.get("title", ""),
                    "summary": entry.get("summary", "")[:200],
                    "published": _parse_time(entry.get("published", "")),
                    "source": source_name,
                })
            print(f"[news] {source_name}: 抓到 {len(feed.entries[:8])} 條")
        except requests.Timeout:
            print(f"[news] 超時跳過: {source_name}")
        except Exception as e:
            print(f"[news] 失敗跳過: {source_name} — {e}")
    return news
