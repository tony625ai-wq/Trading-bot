import os
import asyncio
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YOUR_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

today_decisions = []
auto_trade_enabled = True

def authorised(update: Update) -> bool:
    return update.effective_chat.id == YOUR_CHAT_ID

# ── 每天早上主動推送報告 ──────────────────────────────────
def _e(text: str) -> str:
    """HTML escape AI-generated content"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def send_daily_report(analysis: dict):
    bot = Bot(token=BOT_TOKEN)
    weather = analysis.get("weather", {})

    verdict = weather.get("verdict", "—")
    verdict_emoji = "🟢" if verdict == "BUY DIPS" else "🔴" if verdict == "SELL RIPS" else "🟡"

    # Prompt 1: Weather Check
    weather_text = (
        f"📈 趨勢：{_e(weather.get('trend', '—'))}\n"
        f"😰 波動：{_e(weather.get('volatility', '—'))}\n"
        f"💵 利率：{_e(weather.get('rates', '—'))}\n"
        f"🏆 板塊：{_e(weather.get('leadership', '—'))}\n\n"
        f"{verdict_emoji} <b>今日裁決：{_e(verdict)}</b>\n"
        f"<i>{_e(weather.get('verdict_reason', '—'))}</i>"
    )

    # Top 10 新聞
    top_news = analysis.get("top_news", [])
    news_text = "\n\n".join([
        f"<b>{n.get('rank', i+1)}. {_e(n.get('title', ''))}</b>\n"
        f"🕐 {_e(n.get('published', '—'))} ｜ 📰 {_e(n.get('source', '—'))}\n"
        f"↳ {_e(n.get('reason', '—'))}"
        for i, n in enumerate(top_news[:10])
    ]) or "無新聞資料"

    # Prompt 2+5: 買賣建議 + 真實股價交易指令卡
    cards = []
    for d in analysis.get("decisions", []):
        action = d.get("action", "HOLD")
        emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "⚪️"
        card = f"{emoji} <b>{action} {d['ticker']}</b> x{d.get('quantity', 0)}\n↳ {_e(d.get('reason', '—'))}"

        if action in ("BUY", "SELL") and d.get("current_price") != "N/A":
            ma50_icon = "✅" if d.get("above_ma50") else "❌"
            ma200_icon = "✅" if d.get("above_ma200") else "❌"
            card += (
                f"\n\n📋 <b>交易指令（真實數據）</b>\n"
                f"💰 現價：<b>${_e(d.get('current_price'))}</b>\n"
                f"📈 MA50：${_e(d.get('ma50'))} {ma50_icon}  ｜  MA200：${_e(d.get('ma200'))} {ma200_icon}\n"
                f"📥 入場：市價買入\n"
                f"🛑 止損：<b>${_e(d.get('stop_loss'))}</b>（MA50 或 -7%，取較高）\n"
                f"🎯 目標一：<b>${_e(d.get('target1'))}</b>（+10%，減倉⅓）\n"
                f"🚀 目標二：<b>${_e(d.get('target2'))}</b>（+20% 或 52週高位 ${_e(d.get('week52_high'))}）\n"
                f"⚡️ 催化劑：{_e(d.get('catalysts', '—'))}"
            )
        cards.append(card)

    decisions_text = ("\n\n" + "─"*20 + "\n\n").join(cards) if cards else "今日無建議操作"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 確認執行", callback_data="approve"),
            InlineKeyboardButton("❌ 跳過今日", callback_data="reject"),
        ],
        [InlineKeyboardButton("📊 查看持倉", callback_data="status")]
    ])

    part1 = (
        f"📊 <b>今日美股分析報告</b>\n"
        f"{'─'*28}\n\n"
        f"<b>🌤 Prompt 1｜市場環境</b>\n{weather_text}\n\n"
        f"{'─'*28}\n\n"
        f"<b>📌 今日 Top 10 值得關注新聞</b>\n\n{news_text}"
    )
    part2 = (
        f"<b>💹 Prompt 2｜買賣建議</b>\n{decisions_text}\n\n"
        f"💡 輸入 /analyse AAPL 可對任何股票跑完整 5 Prompt 分析"
    )

    await bot.send_message(chat_id=YOUR_CHAT_ID, text=part1, parse_mode="HTML")
    await bot.send_message(chat_id=YOUR_CHAT_ID, text=part2, parse_mode="HTML", reply_markup=keyboard)

# ── 完整 5-Prompt Stack 報告 ─────────────────────────────
async def send_full_stack_report(ticker: str, result: dict, chat_id: int):
    bot = Bot(token=BOT_TOKEN)

    dd = result.get("deep_dive", {})
    pc = result.get("peer_comparison", {})
    bc = result.get("bear_case", {})
    et = result.get("exit_timer", {})

    # Prompt 2
    p2 = (
        f"<b>🔍 Prompt 2｜Deep Dive — {ticker}</b>\n"
        f"💰 商業模式：{_e(dd.get('business_model', '—'))}\n"
        f"🏰 護城河：{_e(dd.get('moat', '—'))}\n"
        f"⚡️ 催化劑：{_e(dd.get('catalysts', '—'))}\n"
        f"⚖️ 風險回報：{_e(dd.get('asymmetry', '—'))}\n"
        f"➡️ 建議：<b>{_e(dd.get('action', 'HOLD'))}</b> — {_e(dd.get('reason', '—'))}"
    )

    # Prompt 3
    comparison = pc.get("comparison", [])
    comp_text = "\n".join([
        f"• {_e(c.get('ticker',''))}: P/S={_e(c.get('ps_ratio',''))} | 增長={_e(c.get('growth',''))} | V/G={_e(c.get('vg_score',''))} | 毛利={_e(c.get('margin',''))}"
        for c in comparison
    ]) or "未能取得數據"
    p3 = (
        f"<b>⚖️ Prompt 3｜同行比較</b>\n{comp_text}\n"
        f"🏆 最抵買：<b>{_e(pc.get('winner', '—'))}</b> — {_e(pc.get('summary', '—'))}"
    )

    # Prompt 4
    flags = bc.get("red_flags", [])
    flags_text = "\n".join([
        f"{'🔴' if f.get('severity')=='HIGH' else '🟡' if f.get('severity')=='MED' else '🟢'} "
        f"[{f.get('severity')}] {_e(f.get('issue', '—'))}\n  ↳ {_e(f.get('detail', '—'))}"
        for f in flags
    ]) or "未能分析"
    p4 = (
        f"<b>🐻 Prompt 4｜淡倉風險（Bear Case）</b>\n{flags_text}\n"
        f"❗️ 清倉條件：{_e(bc.get('invalidation', '—'))}"
    )

    # Prompt 5
    sl = et.get("stop_loss", {})
    t1 = et.get("target1", {})
    t2 = et.get("target2", {})
    p5 = (
        f"<b>⏱ Prompt 5｜出場計劃（Exit Timer）</b>\n"
        f"🛑 止損：{_e(sl.get('price', '—'))} — {_e(sl.get('reason', '—'))}\n"
        f"🎯 第一目標：{_e(t1.get('price', '—'))}（{_e(t1.get('action', '—'))}）— {_e(t1.get('reason', '—'))}\n"
        f"🚀 最終目標：{_e(t2.get('price', '—'))}（{_e(t2.get('action', '—'))}）— {_e(t2.get('reason', '—'))}\n"
        f"📅 下一催化劑：{_e(et.get('next_catalyst', '—'))}\n"
        f"💡 建議：{_e(et.get('recommendation', '—'))}"
    )

    header = f"<b>📋 {ticker} 完整 5-Prompt Stack 分析</b>\n{'─'*28}\n\n"
    for part in [header + p2, p3, p4, p5]:
        await bot.send_message(chat_id=chat_id, text=part, parse_mode="HTML")

# ── 按鈕回調 ─────────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not authorised(update):
        return

    if query.data == "approve":
        from futu_trader import execute_trades
        from trade_tracker import record_trade
        results = execute_trades(today_decisions, dry_run=False)
        lines = []
        for d in today_decisions:
            if d.get("action") in ("BUY", "SELL") and d.get("current_price") != "N/A":
                record_trade(
                    ticker=d["ticker"], action=d["action"], qty=d.get("quantity", 10),
                    entry_price=d.get("current_price", 0),
                    stop_loss=d.get("stop_loss", 0),
                    target1=d.get("target1", 0),
                    target2=d.get("target2", 0),
                )
                lines.append(f"• {d['action']} {d['ticker']} x{d.get('quantity')} @ ${d.get('current_price')}")
        text = "\n".join(lines) or "今日無操作"
        await query.edit_message_text(f"✅ <b>交易已記錄</b>\n\n{text}\n\n輸入 /pnl 查看持倉損益", parse_mode="HTML")

    elif query.data == "reject":
        await query.edit_message_text("❌ 今日交易已取消，明天見。")

    elif query.data == "status":
        from futu_trader import get_current_portfolio
        portfolio = get_current_portfolio()
        text = "\n".join([f"• {ticker}: {qty} 股" for ticker, qty in portfolio.items()]) or "無持倉"
        await query.edit_message_text(f"📊 *目前持倉*\n\n{text}", parse_mode="Markdown")

# ── /run 指令 ────────────────────────────────────────────
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text("🔄 分析中，大約需要 60-90 秒，請稍候...")
    from main import run_daily_analysis
    await run_daily_analysis()

# ── /analyse TICKER 指令（完整 5-Prompt Stack）──────────────
async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/analyse AAPL")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"🔍 開始對 *{ticker}* 跑完整 5-Prompt Stack 分析，需要約 2-3 分鐘...", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    from news_fetcher import fetch_daily_news
    from ai_analyst import run_full_stack
    news = await loop.run_in_executor(None, fetch_daily_news)
    result = await loop.run_in_executor(None, run_full_stack, ticker, news)
    await send_full_stack_report(ticker, result, update.effective_chat.id)

# ── /status 指令 ─────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from futu_trader import get_current_portfolio
    portfolio = get_current_portfolio()
    text = "\n".join([f"• {ticker}: {qty} 股" for ticker, qty in portfolio.items()]) or "無持倉"
    await update.message.reply_text(f"📊 *目前持倉*\n\n{text}", parse_mode="Markdown")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trade_enabled
    if not authorised(update):
        return
    auto_trade_enabled = False
    await update.message.reply_text("⏸ 自動交易已暫停，輸入 /resume 恢復")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trade_enabled
    if not authorised(update):
        return
    auto_trade_enabled = True
    await update.message.reply_text("▶️ 自動交易已恢復")

async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from trade_tracker import daily_pnl_report
    loop = asyncio.get_event_loop()
    report = await loop.run_in_executor(None, daily_pnl_report)
    trades = report.get("trades", [])
    if not trades:
        await update.message.reply_text("📭 目前無持倉記錄")
        return
    lines = []
    for t in trades:
        pnl = t["pnl_pct"]
        emoji = "🟢" if pnl > 0 else "🔴"
        alert = " ⚠️ 已觸及止損！" if t["hit_stop"] else " 🎯 已觸及目標一！" if t["hit_target1"] else ""
        lines.append(
            f"{emoji} <b>{t['ticker']}</b>  入場：${t['entry']} → 現價：${t['current']}\n"
            f"   損益：<b>{'+' if pnl > 0 else ''}{pnl}%</b>  ｜  止損：${t['stop_loss']}  目標：${t['target1']}{alert}"
        )
    total = report["total_pnl"]
    total_emoji = "🟢" if total > 0 else "🔴"
    text = (
        f"📊 <b>持倉損益報告</b>\n{'─'*24}\n\n"
        + "\n\n".join(lines)
        + f"\n\n{'─'*24}\n{total_emoji} 總損益：<b>{'+' if total > 0 else ''}{total}%</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/add TSLA")
        return
    from watchlist import add, load
    ticker = context.args[0].upper()
    if add(ticker):
        await update.message.reply_text(f"✅ 已加入監察名單：<b>{ticker}</b>\n現有：{', '.join(load())}", parse_mode="HTML")
    else:
        await update.message.reply_text(f"⚠️ {ticker} 已在名單中")

async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/remove TSLA")
        return
    from watchlist import remove, load
    ticker = context.args[0].upper()
    if remove(ticker):
        await update.message.reply_text(f"🗑 已移除：<b>{ticker}</b>\n現有：{', '.join(load())}", parse_mode="HTML")
    else:
        await update.message.reply_text(f"⚠️ {ticker} 不在名單中")

async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from watchlist import load
    tickers = load()
    await update.message.reply_text(f"📋 <b>目前監察名單</b>\n\n{chr(10).join(['• ' + t for t in tickers])}", parse_mode="HTML")

async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    ticker = context.args[0].upper() if context.args else "AAPL"
    await update.message.reply_text(f"⏳ 回測 {ticker} 過去 6 個月，請稍候...")
    loop = asyncio.get_event_loop()
    from backtest import run_backtest
    result = await loop.run_in_executor(None, run_backtest, ticker, 6)
    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return
    win_emoji = "🟢" if result["win_rate"] >= 50 else "🔴"
    pnl_emoji = "🟢" if result["total_pnl"] > 0 else "🔴"
    text = (
        f"📊 <b>{ticker} 回測報告（過去 {result['period_months']} 個月）</b>\n"
        f"{'─'*24}\n"
        f"總交易次數：{result['trades']}\n"
        f"勝：{result['wins']}  負：{result['losses']}\n"
        f"{win_emoji} 勝率：<b>{result['win_rate']}%</b>\n"
        f"平均每筆損益：{result['avg_pnl']}%\n"
        f"{pnl_emoji} 總損益：<b>{result['total_pnl']}%</b>\n\n"
        f"<b>最近 5 筆：</b>\n"
    )
    for t in result.get("trade_log", []):
        e = "✅" if t["result"] == "WIN" else "❌"
        text += f"{e} {t['date']}：{t['pnl']}%\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if len(context.args or []) < 3:
        await update.message.reply_text("用法：/size AAPL 189.50 176.00\n（股票 現價 止損價）")
        return
    try:
        ticker = context.args[0].upper()
        entry = float(context.args[1])
        stop = float(context.args[2])
        portfolio = float(context.args[3]) if len(context.args) > 3 else 50000
        from position_sizer import calculate_position
        pos = calculate_position(portfolio, entry, stop)
        risk_pct = round(abs(entry - stop) / entry * 100, 2)
        await update.message.reply_text(
            f"📐 <b>{ticker} 倉位計算</b>\n"
            f"{'─'*24}\n"
            f"Portfolio：${portfolio:,.0f}\n"
            f"入場價：${entry}  止損：${stop}\n"
            f"每股風險：${pos['risk_per_share']} ({risk_pct}%)\n\n"
            f"✅ 建議買入：<b>{pos['shares']} 股</b>\n"
            f"倉位金額：${pos['position_value']:,.2f}\n"
            f"最大虧損：${pos['risk_amount']:,.2f}（佔 portfolio 2%）",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ 輸入格式錯誤：{e}")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text(
        "📖 <b>指令列表</b>\n\n"
        "📊 <b>分析</b>\n"
        "/run — 立即跑今日市場分析\n"
        "/analyse AAPL — 完整 5-Prompt Stack 分析\n"
        "/backtest AAPL — 回測過去6個月\n\n"
        "💰 <b>交易</b>\n"
        "/pnl — 查看持倉損益\n"
        "/size AAPL 189 176 — 計算倉位大小\n"
        "/status — 查看 Futu 持倉\n\n"
        "📋 <b>名單管理</b>\n"
        "/watchlist — 查看監察名單\n"
        "/add TSLA — 加入股票\n"
        "/remove TSLA — 移除股票\n\n"
        "⚙️ <b>設定</b>\n"
        "/pause — 暫停自動交易\n"
        "/resume — 恢復自動交易\n"
        "/help — 顯示此說明",
        parse_mode="HTML"
    )

async def send_stop_loss_alert(trade: dict):
    bot = Bot(token=BOT_TOKEN)
    await bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=(
            f"🚨 <b>止損警報！</b>\n\n"
            f"<b>{trade['ticker']}</b> 已觸及止損位\n"
            f"入場價：${trade['entry']}  現價：${trade['current']}\n"
            f"止損位：${trade['stop_loss']}\n"
            f"損益：<b>{trade['pnl_pct']}%</b>\n\n"
            f"建議立即出場！"
        ),
        parse_mode="HTML"
    )

async def send_weekly_report(report: dict):
    bot = Bot(token=BOT_TOKEN)
    trades = report.get("trades", [])
    total = report.get("total_pnl", 0)
    if not trades:
        await bot.send_message(chat_id=YOUR_CHAT_ID, text="📊 本週無持倉記錄", parse_mode="HTML")
        return
    lines = []
    for t in trades:
        e = "🟢" if t["pnl_pct"] > 0 else "🔴"
        lines.append(f"{e} {t['ticker']}：{'+' if t['pnl_pct'] > 0 else ''}{t['pnl_pct']}%")
    total_e = "🟢" if total > 0 else "🔴"
    await bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=(
            f"📊 <b>每週績效報告</b>\n{'─'*24}\n\n"
            + "\n".join(lines)
            + f"\n\n{'─'*24}\n{total_e} 總損益：<b>{'+' if total > 0 else ''}{total}%</b>"
        ),
        parse_mode="HTML"
    )

def run_telegram_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("size", cmd_size))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("help", cmd_help))
    print("✅ Telegram Bot 已啟動")
    app.run_polling()
