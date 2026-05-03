import json
import os
import requests
from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
USE_GROQ = bool(GROQ_API_KEY)
MODEL_GROQ = "llama-3.1-8b-instant"
MODEL_OLLAMA = "llama3.2"
OLLAMA_URL = "http://localhost:11434/api/generate"

def _get_watchlist():
    try:
        from watchlist import load
        return load()
    except:
        return ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]

def _ask(prompt: str, timeout: int = 120) -> str:
    # 優先用 Groq（雲端），沒有 key 就用本地 Ollama
    if USE_GROQ:
        try:
            from groq import Groq
            client = Groq(api_key=GROQ_API_KEY)
            resp = client.chat.completions.create(
                model=MODEL_GROQ,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"[ai] Groq 失敗: {e}，嘗試 Ollama...")

    # 本地 Ollama 後備
    try:
        response = requests.post(OLLAMA_URL, json={
            "model": MODEL_OLLAMA,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3}
        }, timeout=timeout)
        response.raise_for_status()
        return response.json()["response"].strip()
    except requests.ConnectionError:
        return "ERROR: Ollama 未啟動"
    except Exception as e:
        return f"ERROR: {e}"

def _parse_json(raw: str) -> dict:
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("No JSON found")
    text = raw[start:end]
    # 修復常見問題：trailing comma、單引號
    import re
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = text.replace("'", '"')
    return json.loads(text)

# ── Prompt 1：Weather Check（市場環境判斷）────────────────
def weather_check(news_list: list[dict]) -> dict:
    news_text = "\n".join([f"- {n['title']}" for n in news_list[:20]])
    raw = _ask(f"""你係專業美股分析師，請用香港繁體中文回答。

根據以下今日新聞，判斷目前市場環境：

新聞：
{news_text}

請只回覆以下JSON格式：
{{
  "trend": "描述SPY大方向，係上升/下跌/橫行趨勢",
  "volatility": "描述市場波動性，恐慌指數VIX係高/中/低",
  "rates": "利率環境，聯儲局係加息/減息/暫停",
  "leadership": "哪些板塊領漲/領跌，係進攻性/防守性主導",
  "verdict": "BUY DIPS、SELL RIPS 或 CASH IS A POSITION（只選一個）",
  "verdict_reason": "一句話解釋原因"
}}""")
    try:
        return _parse_json(raw)
    except:
        return {"trend": "未能分析", "volatility": "未能分析", "rates": "未能分析",
                "leadership": "未能分析", "verdict": "CASH IS A POSITION", "verdict_reason": "數據不足"}

# ── Prompt 2：Deep Dive（深入研究）──────────────────────────
def deep_dive(ticker: str, news_list: list[dict]) -> dict:
    news_text = "\n".join([f"- {n['title']}: {n['summary'][:100]}"
                           for n in news_list if ticker.upper() in n['title'].upper()][:5])
    news_context = f"相關新聞：\n{news_text}" if news_text else ""

    raw = _ask(f"""你係專業美股分析師，請用香港繁體中文回答。

對 {ticker} 進行深入研究分析。{news_context}

請只回覆以下JSON格式：
{{
  "business_model": "佢點樣賺錢？主要收入來源係？",
  "moat": "護城河評級（Strong/Medium/Weak）及原因",
  "catalysts": "未來12個月可能令股價大升或大跌嘅事件",
  "asymmetry": "下行風險 vs 上行潛力分析",
  "action": "BUY、SELL 或 HOLD",
  "reason": "一句話總結建議原因"
}}""")
    try:
        return _parse_json(raw)
    except:
        return {"business_model": "未能分析", "moat": "未能分析", "catalysts": "未能分析",
                "asymmetry": "未能分析", "action": "HOLD", "reason": "數據不足，建議觀望"}

# ── Prompt 3：Peer Comparison（同行比較）────────────────────
def peer_comparison(ticker: str) -> dict:
    peers = {
        "AAPL": ["MSFT", "GOOGL"], "MSFT": ["AAPL", "GOOGL"], "NVDA": ["AMD", "INTC"],
        "GOOGL": ["META", "MSFT"], "AMZN": ["MSFT", "GOOGL"], "META": ["GOOGL", "SNAP"],
        "TSLA": ["GM", "F"], "JPM": ["BAC", "GS"], "V": ["MA", "AXP"],
    }
    peer1, peer2 = peers.get(ticker.upper(), ["SPY", "QQQ"])

    raw = _ask(f"""你係專業美股分析師，請用香港繁體中文回答。

比較 {ticker} 與同行 {peer1}、{peer2} 的估值。

請只回覆以下JSON格式：
{{
  "comparison": [
    {{"ticker": "{ticker}", "ps_ratio": "估計P/S比率", "growth": "估計收入增長%", "vg_score": "P/S除以增長率", "margin": "毛利率估計"}},
    {{"ticker": "{peer1}", "ps_ratio": "估計P/S比率", "growth": "估計收入增長%", "vg_score": "P/S除以增長率", "margin": "毛利率估計"}},
    {{"ticker": "{peer2}", "ps_ratio": "估計P/S比率", "growth": "估計收入增長%", "vg_score": "P/S除以增長率", "margin": "毛利率估計"}}
  ],
  "winner": "V/G Score最低（最抵買）的係？",
  "summary": "一句話總結比較結果"
}}""")
    try:
        return _parse_json(raw)
    except:
        return {"comparison": [], "winner": ticker, "summary": "未能完成同行比較"}

# ── Prompt 4：Bear Case（淡倉風險）──────────────────────────
def bear_case(ticker: str, news_list: list[dict]) -> dict:
    news_text = "\n".join([f"- {n['title']}" for n in news_list if ticker.upper() in n['title'].upper()][:5])

    raw = _ask(f"""你係一個專業淡倉沽空者，請用香港繁體中文回答。

針對 {ticker} 搵出最嚴重嘅風險因素。
{"相關新聞：" + news_text if news_text else ""}

請只回覆以下JSON格式：
{{
  "red_flags": [
    {{"rank": 1, "severity": "HIGH", "issue": "最嚴重風險", "detail": "詳細說明"}},
    {{"rank": 2, "severity": "MED", "issue": "中等風險", "detail": "詳細說明"}},
    {{"rank": 3, "severity": "LOW", "issue": "較低風險", "detail": "詳細說明"}}
  ],
  "invalidation": "如果發生咩事就要完全放棄做多倉？"
}}""")
    try:
        return _parse_json(raw)
    except:
        return {"red_flags": [], "invalidation": "未能分析風險"}

# ── Prompt 5：Exit Timer（出場計劃）─────────────────────────
def exit_timer(ticker: str) -> dict:
    raw = _ask(f"""你係專業美股交易員，請用香港繁體中文回答。

為 {ticker} 制定完整出場計劃。

請只回覆以下JSON格式：
{{
  "stop_loss": {{"price": "止損價位（估計）", "reason": "為何係呢個位止損"}},
  "target1": {{"price": "第一目標價（估計）", "action": "減倉1/3", "reason": "為何係第一目標"}},
  "target2": {{"price": "最終目標價（估計）", "action": "持有剩餘倉位", "reason": "為何係最終目標"}},
  "next_catalyst": "下一個重要催化劑事件（如財報日期等）",
  "recommendation": "係催化劑前減倉定係持有？"
}}""")
    try:
        return _parse_json(raw)
    except:
        return {"stop_loss": {"price": "N/A", "reason": "未能分析"},
                "target1": {"price": "N/A", "action": "N/A", "reason": "未能分析"},
                "target2": {"price": "N/A", "action": "N/A", "reason": "未能分析"},
                "next_catalyst": "未知", "recommendation": "未能分析"}

# ── 完整 5-Prompt Stack（用於 /analyse 指令）────────────────
def run_full_stack(ticker: str, news_list: list[dict]) -> dict:
    print(f"[5-stack] 開始分析 {ticker}...")
    return {
        "ticker": ticker,
        "deep_dive": deep_dive(ticker, news_list),
        "peer_comparison": peer_comparison(ticker),
        "bear_case": bear_case(ticker, news_list),
        "exit_timer": exit_timer(ticker),
    }

# ── 每日報告用（Prompt 1 + 2）───────────────────────────────
def analyse_news(news_list: list[dict], portfolio: dict) -> dict:
    print("[daily] 跑 Weather Check...")
    weather = weather_check(news_list)

    print("[daily] 跑 Top 3 Deep Dive + 真實股價...")
    from price_data import get_price_data
    watchlist = _get_watchlist()
    decisions = []
    for ticker in watchlist[:3]:
        dd = deep_dive(ticker, news_list)
        action = dd.get("action", "HOLD")
        prices = get_price_data(ticker) if action in ("BUY", "SELL") else {}
        decisions.append({
            "ticker": ticker,
            "action": action,
            "quantity": 10 if action == "BUY" else 0,
            "reason": dd.get("reason", "—"),
            "catalysts": dd.get("catalysts", "—"),
            "current_price": prices.get("current_price", "N/A"),
            "ma50": prices.get("ma50", "N/A"),
            "ma200": prices.get("ma200", "N/A"),
            "above_ma50": prices.get("above_ma50", None),
            "above_ma200": prices.get("above_ma200", None),
            "stop_loss": prices.get("stop_loss", "N/A"),
            "target1": prices.get("target1", "N/A"),
            "target2": prices.get("target2", "N/A"),
            "week52_high": prices.get("week52_high", "N/A"),
        })

    # Top 10 新聞
    top_news_raw = _ask(f"""你係財經記者，請用香港繁體中文回答。

從以下新聞中選出最值得注意嘅10條，並說明原因。必須使用新聞原有嘅標題、來源和時間。

新聞列表：
{chr(10).join([f"{i+1}. [{n.get('source','—')}] {n['title']} ({n.get('published','—')})" for i, n in enumerate(news_list[:25])])}

請只回覆以下JSON格式：
{{
  "top_news": [
    {{"rank": 1, "title": "新聞原有標題", "source": "來源名稱", "published": "原有時間", "reason": "為何值得關注"}},
    {{"rank": 2, "title": "新聞原有標題", "source": "來源名稱", "published": "原有時間", "reason": "為何值得關注"}}
  ]
}}""")
    try:
        top_news = _parse_json(top_news_raw).get("top_news", [])
    except:
        top_news = []

    return {
        "weather": weather,
        "decisions": decisions,
        "top_news": top_news,
        "summary": weather.get("verdict_reason", ""),
    }
