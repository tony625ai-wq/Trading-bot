import asyncio
import schedule
import time
import threading
from datetime import datetime, timezone

from news_fetcher import fetch_daily_news
from ai_analyst import analyse_news
from futu_trader import get_current_portfolio
from telegram_control import (
    send_daily_report, send_stop_loss_alert, send_weekly_report,
    today_decisions, auto_trade_enabled, run_telegram_bot
)

# ── 每日分析 ────────────────────────────────────────────
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

# ── 止損監控（每5分鐘，美股交易時間）──────────────────
async def monitor_stop_loss():
    from trade_tracker import daily_pnl_report
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, daily_pnl_report)
    for t in report.get("trades", []):
        if t.get("hit_stop"):
            await send_stop_loss_alert(t)

# ── 每週績效報告（週日 HK 20:00 = UTC 12:00）───────────
async def run_weekly_report():
    from trade_tracker import daily_pnl_report
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, daily_pnl_report)
    await send_weekly_report(report)

# ── 排程器 ──────────────────────────────────────────────
def scheduler_thread():
    # 每天 HK 09:00 = UTC 01:00
    schedule.every().day.at("01:00").do(lambda: asyncio.run(run_daily_analysis()))
    # 美股交易時間（UTC 14:30–21:00）每5分鐘監控止損
    schedule.every(5).minutes.do(lambda: asyncio.run(monitor_stop_loss()))
    # 每週日 HK 20:00 = UTC 12:00 週報
    schedule.every().sunday.at("12:00").do(lambda: asyncio.run(run_weekly_report()))

    print("📅 排程已設定：每天 HK 09:00 自動分析")
    print("🔔 止損監控：每 5 分鐘檢查一次")
    print("📊 每週報告：週日 HK 20:00")
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    t = threading.Thread(target=scheduler_thread, daemon=True)
    t.start()
    run_telegram_bot()
