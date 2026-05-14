import asyncio
import os
import schedule
import time
import threading
from datetime import datetime, timezone

from news_fetcher import fetch_daily_news
from ai_analyst import analyse_news
from futu_trader import get_current_portfolio
from telegram_control import (
    send_daily_report, send_stop_loss_alert, send_weekly_report,
    send_nightly_summary, send_preopen_ranking, send_daily_review, send_autofix_notification,
    today_decisions, auto_trade_enabled, run_telegram_bot
)

# ── 每日分析 ────────────────────────────────────────────────
async def run_daily_analysis():
    print("=== 每日分析開始 ===")
    if not auto_trade_enabled:
        print("自動交易已暫停，跳過")
        return

    loop = asyncio.get_event_loop()
    news = await loop.run_in_executor(None, fetch_daily_news)
    print(f"抓取到 {len(news)} 條新聞")

    portfolio = get_current_portfolio()
    analysis = await loop.run_in_executor(None, analyse_news, news, portfolio)

    today_decisions.clear()
    today_decisions.extend(analysis.get("decisions", []))

    await send_daily_report(analysis)
    print("✅ 報告已發送至 Telegram")

# ── 止損監控 ────────────────────────────────────────────────
async def monitor_stop_loss():
    from trade_tracker import daily_pnl_report
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, daily_pnl_report)
    for t in report.get("trades", []):
        if t.get("hit_stop"):
            await send_stop_loss_alert(t)

# ── 週報 ─────────────────────────────────────────────────────
async def run_weekly_report():
    from trade_tracker import daily_pnl_report
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, daily_pnl_report)
    await send_weekly_report(report)

# ── 日內交易管理 ──────────────────────────────────────────────
def start_day_trading():
    """啟動 HK + US 市場監控 + 日內交易引擎"""
    from watchlist import load as load_watchlist
    import day_trader
    import market_watcher
    import json

    # HK watchlist
    tickers = load_watchlist()
    hk_codes = [f"HK.{t.zfill(5)}" if not t.startswith("HK.") else t for t in tickers[:10]]

    day_trader.start_monitor()
    market_watcher.start_watcher(hk_codes, on_signal=day_trader.on_signal, market="HK")
    print(f"[main] HK day trading started: {hk_codes}")

    # US watchlist
    us_wl_path = os.path.join(os.path.dirname(__file__), "us_watchlist.json")
    try:
        with open(us_wl_path) as f:
            us_codes = json.load(f)
    except Exception:
        us_codes = ["US.NVDA", "US.AAPL", "US.TSLA", "US.AMZN", "US.GOOGL", "US.MSFT"]
    market_watcher.start_watcher(us_codes, on_signal=day_trader.on_signal, market="US")
    print(f"[main] US day trading started: {us_codes}")

_nightly_sent_date = None

def _check_nightly_summary():
    global _nightly_sent_date
    try:
        import pytz
        uk  = pytz.timezone("Europe/London")
        now = datetime.now(uk)
        today = now.date()
        if now.hour == 22 and now.minute < 5 and _nightly_sent_date != today:
            _nightly_sent_date = today
            asyncio.run(send_nightly_summary())
            print("[nightly] 每晚總結已發送")
    except Exception as e:
        print(f"[nightly] 發送失敗: {e}")

_daily_review_sent_date = None
_autofix_notified_date = None

def _check_autofix_notification():
    global _autofix_notified_date
    try:
        import pytz
        uk = pytz.timezone("Europe/London")
        now = datetime.now(uk)
        today = now.date()
        if now.hour == 23 and now.minute >= 15 and now.minute < 20 and _autofix_notified_date != today:
            _autofix_notified_date = today
            asyncio.run(send_autofix_notification())
    except Exception as e:
        print(f"[autofix-notify] 排程失敗: {e}")

def _check_daily_review():
    global _daily_review_sent_date
    try:
        import pytz
        uk  = pytz.timezone("Europe/London")
        now = datetime.now(uk)
        today = now.date()
        if now.hour == 22 and now.minute >= 30 and now.minute < 35 and _daily_review_sent_date != today:
            _daily_review_sent_date = today
            asyncio.run(send_daily_review())
            print("[review] 每日績效報告已發送")
    except Exception as e:
        print(f"[review] 發送失敗: {e}")


def _fetch_southbound_flow() -> float:
    """Fetch today's southbound (mainland→HK) net buy flow in HKD millions via Futu capital flow."""
    try:
        from futu import OpenQuoteContext, RET_OK
        with OpenQuoteContext(host='127.0.0.1', port=11111) as ctx:
            # get_capital_flow returns main-force/super-large/large/mid/small flow
            ret, data = ctx.get_capital_flow("HK.HSI")
            if ret == RET_OK and not data.empty:
                # 'main_inflow' is the large-capital net inflow proxy for southbound
                inflow = float(data.get("main_inflow", [0]).iloc[-1] if hasattr(data.get("main_inflow"), "iloc") else 0)
                print(f"[southbound] HSI capital flow: {inflow:+.0f}M HKD")
                return inflow
    except Exception:
        pass
    # Fallback: fetch from HKEX public data
    try:
        import urllib.request, json as _json
        url = "https://www.hkex.com.hk/eng/csm/data/southbound.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            d = _json.loads(r.read())
            flow = float(d.get("net", 0))
            print(f"[southbound] HKEX net flow: {flow:+.0f}M HKD")
            return flow
    except Exception:
        pass
    return 0.0


def _send_preopen_ranking():
    """Rank watchlist stocks by gap at market open via Futu snapshot, then update southbound bias."""
    try:
        from futu import OpenQuoteContext, RET_OK
        from watchlist import load
        import day_trader
        tickers = load()
        codes = [f"HK.{t.zfill(5)}" if not t.startswith("HK.") else t for t in tickers]
        rankings = []
        with OpenQuoteContext(host='127.0.0.1', port=11111) as ctx:
            ret, data = ctx.get_market_snapshot(codes)
            if ret == RET_OK and not data.empty:
                for _, row in data.iterrows():
                    last = float(row.get("last_price", 0) or 0)
                    prev = float(row.get("prev_close_price", 0) or 0)
                    if last > 0 and prev > 0:
                        gap = round((last - prev) / prev * 100, 2)
                        rankings.append({"code": row["code"], "price": last, "gap_pct": gap})
        rankings.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)

        # Set southbound directional bias in day_trader
        sb_flow = _fetch_southbound_flow()
        day_trader.set_southbound_bias(sb_flow)

        asyncio.run(send_preopen_ranking(rankings, sb_flow))
        print(f"[preopen] 選股排名已發送，Top: {rankings[0]['code'] if rankings else 'N/A'}")
    except Exception as e:
        print(f"[preopen] 排名失敗: {e}")


def _futu_health_check():
    try:
        from futu import OpenQuoteContext, RET_OK
        with OpenQuoteContext(host='127.0.0.1', port=11111) as ctx:
            ret, _ = ctx.get_global_state()
            if ret != RET_OK:
                raise Exception("get_global_state failed")
    except Exception as e:
        try:
            from dotenv import load_dotenv
            from telegram import Bot
            load_dotenv()
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
            if bot_token and chat_id:
                asyncio.run(Bot(token=bot_token).send_message(
                    chat_id=chat_id,
                    text=f"🚨 <b>FutuOpenD 連線異常！</b>\n\n{e}\n\n請立即重啟 FutuOpenD，否則日內交易無法執行",
                    parse_mode="HTML"
                ))
        except:
            pass
        print(f"[health] FutuOpenD 連線異常: {e}")


def schedule_market_close_cleanup():
    """Register HK + US daily close-all and reset schedules"""
    # ── HK ──
    # HK 15:55 HKT = 07:55 UTC
    schedule.every().day.at("07:55").do(_close_hk_intraday)
    # Reset intraday state at HK 09:30 HKT = 01:30 UTC
    schedule.every().day.at("01:30").do(_reset_intraday)
    # HK pre-open ranking at 09:35 HKT = 01:35 UTC
    schedule.every().day.at("01:35").do(_send_preopen_ranking)

    # ── US ──
    # US 15:55 ET = 20:55 UTC (EST) / 19:55 UTC (EDT — clocks back Nov-Mar)
    # Use 20:55 UTC as safe year-round target (fires 1 min early during summer)
    schedule.every().day.at("20:55").do(_close_us_intraday)
    # US pre-open ranking at 09:35 ET = 14:35 UTC (EDT) / 13:35 UTC (EST)
    # Schedule at 14:35 UTC — catches EDT, fires 1 hr early in EST (fine)
    schedule.every().day.at("14:35").do(_send_us_preopen_ranking)


def _close_hk_intraday():
    import day_trader, market_watcher
    day_trader.close_all_intraday("HK 收市前自動平倉（HK 倉）", market_filter="HK")
    market_watcher.reset_daily()

def _close_us_intraday():
    import day_trader
    day_trader.close_all_intraday("US 收市前自動平倉（US 倉）", market_filter="US")

def _reset_intraday():
    import market_watcher, day_trader
    market_watcher.reset_daily()
    day_trader.reset_intraday_state()
    print("[main] 新交易日開始，日內狀態已重置")

def _send_us_preopen_ranking():
    """Rank US watchlist by pre-market gap and send to Telegram."""
    try:
        import json
        from futu import OpenQuoteContext, RET_OK
        us_wl_path = os.path.join(os.path.dirname(__file__), "us_watchlist.json")
        with open(us_wl_path) as f:
            us_codes = json.load(f)
        rankings = []
        with OpenQuoteContext(host='127.0.0.1', port=11111) as ctx:
            ret, data = ctx.get_market_snapshot(us_codes)
            if ret == RET_OK and not data.empty:
                for _, row in data.iterrows():
                    last = float(row.get("last_price", 0) or 0)
                    prev = float(row.get("prev_close_price", 0) or 0)
                    if last > 0 and prev > 0:
                        gap = round((last - prev) / prev * 100, 2)
                        rankings.append({"code": row["code"], "price": last, "gap_pct": gap})
        rankings.sort(key=lambda x: abs(x["gap_pct"]), reverse=True)
        asyncio.run(send_preopen_ranking(rankings, 0.0))
        print(f"[us-preopen] US 選股排名已發送，Top: {rankings[0]['code'] if rankings else 'N/A'}")
    except Exception as e:
        print(f"[us-preopen] 排名失敗: {e}")

# ── 排程器 ──────────────────────────────────────────────────
def scheduler_thread():
    # HK 08:30 = UTC 00:30 — 開市前 30 分鐘跑分析
    schedule.every().day.at("00:30").do(lambda: asyncio.run(run_daily_analysis()))
    # FutuOpenD 健康監測，每 15 分鐘一次
    schedule.every(15).minutes.do(_futu_health_check)
    # 每晚 10 PM UK 時間總結
    schedule.every(1).minutes.do(_check_nightly_summary)
    schedule.every(1).minutes.do(_check_daily_review)
    schedule.every(1).minutes.do(_check_autofix_notification)
    # 止損監控每 15 分鐘一次（降低 CPU 佔用）
    schedule.every(15).minutes.do(lambda: asyncio.run(monitor_stop_loss()))
    # 週日 HK 20:00 = UTC 12:00 週報
    schedule.every().sunday.at("12:00").do(lambda: asyncio.run(run_weekly_report()))

    schedule_market_close_cleanup()

    print("📅 排程：每天 HK 09:00 自動分析")
    print("🔔 止損監控：每 5 分鐘")
    print("📊 週報：週日 HK 20:00")
    print("🏃 HK 收市平倉：每天 HK 15:55 (UTC 07:55)")
    print("🏃 US 收市平倉：每天 ET 15:55 (UTC 20:55)")
    print("📈 HK 開市排名：每天 HK 09:35 (UTC 01:35)")
    print("📈 US 開市排名：每天 ET 09:35 (UTC 14:35)")
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    # Scheduler thread
    t = threading.Thread(target=scheduler_thread, daemon=True)
    t.start()

    # Day trading thread
    dt = threading.Thread(target=start_day_trading, daemon=True)
    dt.start()

    # Real-time news monitor
    import news_monitor
    loop = asyncio.new_event_loop()
    news_monitor.start_monitor(loop)

    run_telegram_bot()
