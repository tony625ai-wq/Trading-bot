import os
import asyncio
from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageReactionHandler, ContextTypes

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YOUR_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

today_decisions = []
auto_trade_enabled = True
_approval_msg_id: int | None = None

def authorised(update: Update) -> bool:
    return update.effective_chat.id == YOUR_CHAT_ID

def _e(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ── 每天早上主動推送報告 ──────────────────────────────────
async def send_daily_report(analysis: dict):
    bot = Bot(token=BOT_TOKEN)
    weather = analysis.get("weather", {})

    verdict = weather.get("verdict", "—")
    verdict_emoji = "🟢" if verdict == "BUY DIPS" else "🔴" if verdict == "SELL RIPS" else "🟡"

    weather_text = (
        f"📈 趨勢：{_e(weather.get('trend', '—'))}\n"
        f"😰 波動：{_e(weather.get('volatility', '—'))}\n"
        f"💵 利率：{_e(weather.get('rates', '—'))}\n"
        f"🏆 板塊：{_e(weather.get('leadership', '—'))}\n\n"
        f"{verdict_emoji} <b>今日裁決：{_e(verdict)}</b>\n"
        f"<i>{_e(weather.get('verdict_reason', '—'))}</i>"
    )

    top_news = analysis.get("top_news", [])
    news_text = "\n\n".join([
        f"<b>{n.get('rank', i+1)}. {_e(n.get('title', ''))}</b>\n"
        f"🕐 {_e(n.get('published', '—'))} ｜ 📰 {_e(n.get('source', '—'))}\n"
        f"↳ {_e(n.get('reason', '—'))}"
        for i, n in enumerate(top_news[:10])
    ]) or "無新聞資料"

    cards = []
    for d in analysis.get("decisions", []):
        action = d.get("action", "HOLD")
        emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "⚪️"
        card = f"{emoji} <b>{action} {d['ticker']}</b> x{d.get('quantity', 0)}\n↳ {_e(d.get('reason', '—'))}"

        # Signal Score bar
        sig = d.get("signal_score", 0)
        try:
            sig = int(sig)
        except:
            sig = 0
        filled = "█" * sig + "░" * (10 - sig)
        sig_emoji = "🔥" if sig >= 8 else "✅" if sig >= 5 else "⚠️"
        card += f"\n\n{sig_emoji} <b>訊號強度：{sig}/10</b>  [{filled}]"

        if d.get("current_price") != "N/A":
            ma50_icon = "✅" if d.get("above_ma50") else "❌"
            ma200_icon = "✅" if d.get("above_ma200") else "❌"
            card += (
                f"\n\n📋 <b>交易指令</b>\n"
                f"💰 現價：<b>${_e(d.get('current_price'))}</b>\n"
                f"📈 MA50：${_e(d.get('ma50'))} {ma50_icon}  ｜  MA200：${_e(d.get('ma200'))} {ma200_icon}\n"
                f"🛑 止損：<b>${_e(d.get('stop_loss'))}</b>  ｜  🎯 目標一：<b>${_e(d.get('target1'))}</b>  ｜  🚀 目標二：<b>${_e(d.get('target2'))}</b>\n"
                f"⚡️ 催化劑：{_e(d.get('catalysts', '—'))}"
            )

            # 技術指標
            rsi = d.get("rsi", "N/A")
            macd_bullish = d.get("macd_bullish")
            tech_score = d.get("tech_score", "N/A")
            if rsi != "N/A":
                try:
                    rsi_val = float(rsi)
                    rsi_icon = "🔥 過熱" if rsi_val > 70 else "🧊 超賣" if rsi_val < 30 else "✅ 正常"
                except:
                    rsi_icon = ""
                macd_icon = "📈 金叉" if macd_bullish else "📉 死叉"
                card += (
                    f"\n\n📊 <b>技術指標</b>\n"
                    f"RSI：<b>{_e(rsi)}</b> {rsi_icon}  ｜  MACD：{macd_icon}\n"
                    f"布林帶：${_e(d.get('bb_lower'))} — ${_e(d.get('bb_upper'))}\n"
                    f"技術評分：<b>{_e(tech_score)}</b>"
                )

        cards.append(card)

    decisions_text = ("\n\n" + "─" * 20 + "\n\n").join(cards) if cards else "今日無建議操作"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 確認執行", callback_data="approve"),
            InlineKeyboardButton("❌ 跳過今日", callback_data="reject"),
        ],
        [InlineKeyboardButton("📊 查看持倉", callback_data="status")]
    ])

    part1 = (
        f"📊 <b>今日美股分析報告</b>\n"
        f"{'─' * 28}\n\n"
        f"<b>🌤 市場環境</b>\n{weather_text}\n\n"
        f"{'─' * 28}\n\n"
        f"<b>📌 今日 Top 10 值得關注新聞</b>\n\n{news_text}"
    )
    part2 = (
        f"<b>💹 買賣建議 + 技術指標</b>\n{decisions_text}\n\n"
        f"💡 輸入 /analyse AAPL 可對任何股票跑完整 5 Prompt 分析"
    )

    await bot.send_message(chat_id=YOUR_CHAT_ID, text=part1, parse_mode="HTML")
    msg = await bot.send_message(chat_id=YOUR_CHAT_ID, text=part2, parse_mode="HTML", reply_markup=keyboard)
    global _approval_msg_id
    _approval_msg_id = msg.message_id

# ── 完整 5-Prompt Stack 報告 ─────────────────────────────
async def send_full_stack_report(ticker: str, result: dict, chat_id: int):
    bot = Bot(token=BOT_TOKEN)

    dd = result.get("deep_dive", {})
    pc = result.get("peer_comparison", {})
    bc = result.get("bear_case", {})
    et = result.get("exit_timer", {})

    p2 = (
        f"<b>🔍 Deep Dive — {ticker}</b>\n"
        f"💰 商業模式：{_e(dd.get('business_model', '—'))}\n"
        f"🏰 護城河：{_e(dd.get('moat', '—'))}\n"
        f"⚡️ 催化劑：{_e(dd.get('catalysts', '—'))}\n"
        f"⚖️ 風險回報：{_e(dd.get('asymmetry', '—'))}\n"
        f"➡️ 建議：<b>{_e(dd.get('action', 'HOLD'))}</b> — {_e(dd.get('reason', '—'))}"
    )

    comparison = pc.get("comparison", [])
    comp_text = "\n".join([
        f"• {_e(c.get('ticker',''))}: P/S={_e(c.get('ps_ratio',''))} | 增長={_e(c.get('growth',''))} | V/G={_e(c.get('vg_score',''))} | 毛利={_e(c.get('margin',''))}"
        for c in comparison
    ]) or "未能取得數據"
    p3 = (
        f"<b>⚖️ 同行比較</b>\n{comp_text}\n"
        f"🏆 最抵買：<b>{_e(pc.get('winner', '—'))}</b> — {_e(pc.get('summary', '—'))}"
    )

    flags = bc.get("red_flags", [])
    flags_text = "\n".join([
        f"{'🔴' if f.get('severity')=='HIGH' else '🟡' if f.get('severity')=='MED' else '🟢'} "
        f"[{f.get('severity')}] {_e(f.get('issue', '—'))}\n  ↳ {_e(f.get('detail', '—'))}"
        for f in flags
    ]) or "未能分析"
    p4 = (
        f"<b>🐻 淡倉風險（Bear Case）</b>\n{flags_text}\n"
        f"❗️ 清倉條件：{_e(bc.get('invalidation', '—'))}"
    )

    sl = et.get("stop_loss", {})
    t1 = et.get("target1", {})
    t2 = et.get("target2", {})
    p5 = (
        f"<b>⏱ 出場計劃（Exit Timer）</b>\n"
        f"🛑 止損：{_e(sl.get('price', '—'))} — {_e(sl.get('reason', '—'))}\n"
        f"🎯 第一目標：{_e(t1.get('price', '—'))}（{_e(t1.get('action', '—'))}）— {_e(t1.get('reason', '—'))}\n"
        f"🚀 最終目標：{_e(t2.get('price', '—'))}（{_e(t2.get('action', '—'))}）— {_e(t2.get('reason', '—'))}\n"
        f"📅 下一催化劑：{_e(et.get('next_catalyst', '—'))}\n"
        f"💡 建議：{_e(et.get('recommendation', '—'))}"
    )

    header = f"<b>📋 {ticker} 完整 5-Prompt Stack 分析</b>\n{'─' * 28}\n\n"
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
        await query.edit_message_text(f"📊 <b>目前持倉</b>\n\n{text}", parse_mode="HTML")

# ── /run 指令 ────────────────────────────────────────────
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text("🔄 分析中，大約需要 60-90 秒，請稍候...")
    from main import run_daily_analysis
    await run_daily_analysis()

# ── /analyse TICKER ──────────────────────────────────────
async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/analyse AAPL")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"🔍 開始對 <b>{ticker}</b> 跑完整 5-Prompt Stack，需要約 2-3 分鐘...", parse_mode="HTML")

    loop = asyncio.get_event_loop()
    from news_fetcher import fetch_daily_news
    from ai_analyst import run_full_stack
    news = await loop.run_in_executor(None, fetch_daily_news)
    result = await loop.run_in_executor(None, run_full_stack, ticker, news)
    await send_full_stack_report(ticker, result, update.effective_chat.id)

# ── /close TICKER [exit_price] ───────────────────────────
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/close AAPL\n      /close AAPL 189.50（指定出場價）")
        return

    ticker = context.args[0].upper()
    manual_price = float(context.args[1]) if len(context.args) > 1 else None

    from trade_tracker import get_open_trades, close_trade
    open_trades = get_open_trades()
    matching = [t for t in open_trades if t["ticker"] == ticker]

    if not matching:
        await update.message.reply_text(f"⚠️ {ticker} 無開倉記錄")
        return

    trade = matching[-1]

    if manual_price:
        exit_price = manual_price
    else:
        from price_data import get_price_data
        prices = get_price_data(ticker)
        exit_price = prices.get("current_price", 0)
        if not exit_price:
            await update.message.reply_text(f"❌ 無法取得 {ticker} 當前價格，請手動輸入：/close {ticker} <價格>")
            return

    close_trade(trade["id"], exit_price)
    pnl = ((exit_price - trade["entry_price"]) / trade["entry_price"]) * 100
    if trade["action"] == "SELL":
        pnl = -pnl
    pnl = round(pnl, 2)
    emoji = "🟢" if pnl > 0 else "🔴"

    await update.message.reply_text(
        f"{emoji} <b>{ticker} 已平倉</b>\n"
        f"{'─' * 20}\n"
        f"入場：${trade['entry_price']}  →  出場：${exit_price}\n"
        f"持倉：{trade.get('qty', '—')} 股  ｜  方向：{trade['action']}\n"
        f"開倉日：{trade.get('opened_at', '—')[:10]}\n\n"
        f"損益：<b>{'+' if pnl > 0 else ''}{pnl}%</b>\n\n"
        f"輸入 /pnl 查看所有持倉狀態",
        parse_mode="HTML"
    )

# ── /benchmark ───────────────────────────────────────────
async def cmd_benchmark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text("📊 計算 Portfolio vs S&P 500，請稍候...")

    loop = asyncio.get_event_loop()
    from benchmark import get_benchmark_report
    report = await loop.run_in_executor(None, get_benchmark_report)

    if "error" in report:
        await update.message.reply_text(f"❌ {report['error']}")
        return

    beating = report["beating_market"]
    beat_emoji = "🏆 跑贏大市！" if beating else "📉 落後大市"
    alpha = report["alpha"]
    alpha_str = f"+{alpha}%" if alpha > 0 else f"{alpha}%"
    port_str = f"+{report['portfolio_return']}%" if report["portfolio_return"] > 0 else f"{report['portfolio_return']}%"
    spy_str = f"+{report['spy_return']}%" if report["spy_return"] > 0 else f"{report['spy_return']}%"

    hsi_return = report.get("hsi_return")
    hsi_str = (f"{'+' if hsi_return > 0 else ''}{hsi_return}%" if hsi_return is not None else "N/A")

    await update.message.reply_text(
        f"📊 <b>Portfolio vs 大市</b>\n"
        f"{'─' * 24}\n"
        f"統計起點：{report['start_date']}\n\n"
        f"🗂 總交易：{report['total_trades']} 筆（已平倉 {report['closed_trades']}，持倉中 {report['open_trades']}）\n\n"
        f"💼 我的 Portfolio：<b>{port_str}</b>\n"
        f"   ↳ 已平倉 PnL：{'+' if report['closed_pnl'] > 0 else ''}{report['closed_pnl']}%\n"
        f"   ↳ 持倉浮動：{'+' if report['open_pnl'] > 0 else ''}{report['open_pnl']}%\n\n"
        f"🇭🇰 恒生指數（HSI）：<b>{hsi_str}</b>\n"
        f"🇺🇸 S&P 500（SPY）：<b>{spy_str}</b>\n\n"
        f"{'─' * 24}\n"
        f"vs SPY Alpha：<b>{alpha_str}</b>  {beat_emoji}",
        parse_mode="HTML"
    )

# ── /balance 指令 ────────────────────────────────────────
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text("💰 查詢模擬賬戶資金，請稍候...")
    loop = asyncio.get_event_loop()
    from futu_trader import get_account_info
    info = await loop.run_in_executor(None, get_account_info)

    lines = []
    for market_label, label_name, flag in [("hk", "港股模擬賬戶", "🇭🇰"), ("us", "美股模擬賬戶", "🇺🇸")]:
        acc = info.get(market_label)
        if acc:
            currency = acc["currency"]
            lines.append(
                f"{flag} <b>{label_name}</b>（ID: {acc['acc_id']}）\n"
                f"   購買力：<b>{acc['power']:,.2f} {currency}</b>\n"
                f"   現金：{acc['cash']:,.2f} {currency}\n"
                f"   證券市值：{acc['market_val']:,.2f} {currency}\n"
                f"   總資產：{acc['total_assets']:,.2f} {currency}"
            )
        else:
            flag2 = "🇭🇰" if market_label == "hk" else "🇺🇸"
            lines.append(f"{flag2} <b>{label_name}</b>：未能連線（OpenD 是否已登入？）")

    if not lines:
        await update.message.reply_text("❌ 無法取得賬戶資訊，請確認 FutuOpenD 已啟動並登入")
        return

    await update.message.reply_text(
        f"💰 <b>模擬賬戶資金</b>\n{'─' * 24}\n\n" + "\n\n".join(lines),
        parse_mode="HTML"
    )

# ── /status 指令 ─────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from futu_trader import get_current_portfolio
    portfolio = get_current_portfolio()
    if not portfolio:
        await update.message.reply_text("📊 <b>目前持倉</b>\n\n（無持倉或 OpenD 未連線）", parse_mode="HTML")
        return
    hk = {k: v for k, v in portfolio.items() if k.startswith("HK.")}
    us = {k: v for k, v in portfolio.items() if k.startswith("US.")}
    lines = []
    if hk:
        lines.append("<b>🇭🇰 港股</b>")
        lines += [f"• {ticker}: {qty} 股" for ticker, qty in hk.items()]
    if us:
        lines.append("\n<b>🇺🇸 美股</b>")
        lines += [f"• {ticker}: {qty} 股" for ticker, qty in us.items()]
    await update.message.reply_text(f"📊 <b>目前持倉</b>\n\n" + "\n".join(lines), parse_mode="HTML")

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
        f"📊 <b>持倉損益報告</b>\n{'─' * 24}\n\n"
        + "\n\n".join(lines)
        + f"\n\n{'─' * 24}\n{total_emoji} 總損益：<b>{'+' if total > 0 else ''}{total}%</b>"
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
        f"{'─' * 24}\n"
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
            f"{'─' * 24}\n"
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

async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from trade_journal import get_stats, _load_journal
    stats = get_stats()
    if stats["trades"] == 0:
        await update.message.reply_text("📓 交易日誌：暫無記錄")
        return

    by_sig = stats.get("by_signal", {})
    sig_lines = "\n".join([
        f"  {s}：{v['trades']} 筆，勝 {v['wins']}，均損益 {v['avg']:+.1f}%"
        for s, v in by_sig.items()
    ]) or "  無"

    win_e = "🟢" if stats["win_rate"] >= 50 else "🔴"
    pnl_e = "🟢" if stats["total_pnl"] > 0 else "🔴"

    # Last 5 closed trades
    journal = _load_journal()
    closed = [t for t in journal if t["outcome"]][-5:]
    trade_lines = []
    for t in reversed(closed):
        e = "✅" if t["outcome"] == "WIN" else "❌"
        lesson = t.get("lesson", "")
        lesson_str = f"\n    💡 {_e(lesson[:60])}" if lesson else ""
        trade_lines.append(
            f"{e} {t['ticker']}  {t['signal_type']}  {'+' if t['pnl_pct'] > 0 else ''}{t['pnl_pct']}%{lesson_str}"
        )

    await update.message.reply_text(
        f"📓 <b>交易日誌</b>\n{'─'*24}\n"
        f"總交易：{stats['trades']}  勝：{stats['wins']}  負：{stats['losses']}\n"
        f"{win_e} 勝率：<b>{stats['win_rate']}%</b>  {pnl_e} 總損益：<b>{'+' if stats['total_pnl'] > 0 else ''}{stats['total_pnl']}%</b>\n\n"
        f"<b>按信號類型：</b>\n{sig_lines}\n\n"
        f"<b>最近 5 筆：</b>\n" + "\n".join(trade_lines),
        parse_mode="HTML"
    )

async def cmd_lessons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from trade_journal import get_recent_lessons, _load_lessons
    lessons = _load_lessons()
    if not lessons:
        await update.message.reply_text("📚 教訓庫：暫無記錄，等第一批交易完成後 AI 會自動覆盤")
        return
    recent = sorted(lessons, key=lambda x: x.get("recorded_at", ""), reverse=True)[:10]
    lines = []
    for i, l in enumerate(recent):
        e = "✅" if l["outcome"] == "WIN" else "❌"
        lines.append(
            f"{e} <b>[{l['lesson_type'].upper()}]</b> {_e(l['lesson'])}\n"
            f"   來自：{l['ticker']}  {'+' if l['pnl_pct'] > 0 else ''}{l['pnl_pct']}%  ({l['signal_type']})"
        )
    await update.message.reply_text(
        f"📚 <b>AI 交易教訓庫（最近 {len(recent)} 條）</b>\n{'─'*24}\n\n" + "\n\n".join(lines),
        parse_mode="HTML"
    )

async def cmd_daytrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    import day_trader
    if context.args and context.args[0].lower() in ("off", "stop", "pause"):
        day_trader.set_enabled(False)
        await update.message.reply_text("⏸ 日內交易已停止")
    elif context.args and context.args[0].lower() in ("on", "start", "resume"):
        day_trader.set_enabled(True)
        await update.message.reply_text("▶️ 日內交易已恢復")
    else:
        positions = day_trader.get_open_positions()
        enabled = day_trader.is_enabled()
        status = "🟢 運行中" if enabled else "🔴 已停止"
        if positions:
            lines = [f"• {p['code']}  x{p['qty']}  入場:{p['entry']}  止:{p['stop']}  目標:{p['target']}" for p in positions]
            pos_text = "\n".join(lines)
        else:
            pos_text = "無持倉"
        await update.message.reply_text(
            f"🏃 <b>日內交易狀態：{status}</b>\n{'─'*20}\n{pos_text}\n\n"
            f"/daytrade on — 開啟\n/daytrade off — 停止",
            parse_mode="HTML"
        )

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text("📊 分析持倉配置，請稍候...")
    loop = asyncio.get_event_loop()
    from futu_trader import get_current_portfolio, get_account_info
    from portfolio_manager import get_portfolio_analysis

    portfolio = await loop.run_in_executor(None, get_current_portfolio)
    acct      = await loop.run_in_executor(None, get_account_info)

    if not portfolio:
        await update.message.reply_text("📭 目前無持倉，或 FutuOpenD 未連線")
        return

    result = await loop.run_in_executor(None, get_portfolio_analysis, portfolio, acct)
    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return

    # Core/satellite bar
    c = result["core_pct"]; s = result["satellite_pct"]; cash = result["cash_pct"]
    bar = f"核心 {c}% | 衛星 {s}% | 現金 {cash}%"

    # Positions
    pos_lines = []
    for p in result["positions"][:10]:
        tag   = "🔵" if p["is_core"] else "🟡"
        alert = " ⚠️" if p["alerts"] else ""
        pos_lines.append(
            f"{tag} <b>{p['code'].replace('HK.','').replace('US.','')}</b> "
            f"x{p['qty']}  {p['weight_pct']}%  HKD {p['value_hkd']:,.0f}{alert}"
        )

    # Sector breakdown
    sector_lines = [f"  {k}: {v}%" for k, v in sorted(result["sector_totals"].items(), key=lambda x: -x[1])]

    all_alerts = result["drift_alerts"] + result["sector_alerts"]
    for p in result["positions"]:
        all_alerts += p["alerts"]
    alert_text = "\n".join(all_alerts) if all_alerts else "✅ 所有倉位符合風險規則"

    await update.message.reply_text(
        f"📊 <b>Portfolio 配置分析</b>\n{'─' * 24}\n\n"
        f"💰 總資產：HKD {result['total_hkd']:,.0f}\n"
        f"🎯 配置：{bar}\n\n"
        f"<b>持倉明細：</b>\n" + "\n".join(pos_lines) +
        f"\n\n<b>板塊分布：</b>\n" + "\n".join(sector_lines) +
        f"\n\n<b>風險警報：</b>\n{alert_text}\n\n"
        f"💡 目標：核心 65% | 衛星 30% | 現金 5%\n"
        f"📌 /sharpe 查看夏普比率",
        parse_mode="HTML"
    )


async def cmd_sharpe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    loop = asyncio.get_event_loop()
    from portfolio_manager import get_sharpe_ratio
    result = await loop.run_in_executor(None, get_sharpe_ratio)
    if "error" in result:
        await update.message.reply_text(f"📉 {result['error']}")
        return
    await update.message.reply_text(
        f"📈 <b>Portfolio 夏普比率</b>\n{'─' * 24}\n\n"
        f"分析交易：{result['trades']} 筆\n"
        f"平均每筆回報：{result['avg_return_pct']:+.2f}%\n"
        f"波動率（標準差）：{result['std_pct']:.2f}%\n"
        f"年化回報估算：{result['annual_return_pct']:+.1f}%\n\n"
        f"🏆 <b>夏普比率：{result['sharpe_ratio']}</b>  {result['rating']}\n"
        f"✅ 勝率：{result['win_rate']}%\n\n"
        f"<i>夏普比率 >2 = 優秀，>1 = 良好，>0 = 合格</i>",
        parse_mode="HTML"
    )


async def cmd_fundamentals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/fundamentals 0700\n      /fundamentals AAPL")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"📋 抓取 <b>{ticker}</b> 基本面數據...", parse_mode="HTML")
    loop = asyncio.get_event_loop()
    from price_data import get_price_data
    d = await loop.run_in_executor(None, get_price_data, ticker)
    if not d:
        await update.message.reply_text(f"❌ 無法取得 {ticker} 數據")
        return
    pe = d.get("pe_ratio", "N/A")
    try:
        pe_note = "📉 偏低（<15 可能低估）" if float(pe) < 15 else "📈 偏高（>30 需謹慎）" if float(pe) > 30 else "✅ 合理"
    except Exception:
        pe_note = ""
    await update.message.reply_text(
        f"📋 <b>{ticker} 基本面</b>\n{'─' * 24}\n\n"
        f"💰 現價：${_e(d.get('current_price','N/A'))}\n\n"
        f"📊 <b>估值</b>\n"
        f"市盈率 (PE)：<b>{_e(pe)}</b> {pe_note}\n"
        f"每股盈利 (EPS)：{_e(d.get('eps','N/A'))}\n\n"
        f"💼 <b>盈利能力</b>\n"
        f"股本回報率 (ROE)：{_e(d.get('roe','N/A'))}\n"
        f"利潤率：{_e(d.get('profit_margin','N/A'))}\n\n"
        f"📅 下次業績：<b>{_e(d.get('earnings_date','N/A'))}</b>\n\n"
        f"💡 輸入 /analyse {ticker} 睇完整 AI 分析",
        parse_mode="HTML"
    )


async def cmd_safety(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from safety_check import get_last_decisions
    decisions = get_last_decisions(5)
    if not decisions:
        await update.message.reply_text("📋 尚無 Safety Check 記錄")
        return
    lines = []
    for d in reversed(decisions):
        ts = d.get("timestamp", "")[:16].replace("T", " ")
        code = d.get("symbol", "—")
        all_pass = d.get("all_pass", False)
        result_icon = "✅" if all_pass else "🚫"
        mode = "PAPER" if d.get("paper_trading") else "LIVE"
        ind = d.get("indicators", {})
        failed = [c["label"] for c in d.get("conditions", []) if not c["pass"]]
        fail_text = "\n   ".join(failed) if failed else "—"
        lines.append(
            f"{result_icon} <b>{code}</b>  {ts} UTC  [{mode}]\n"
            f"   現價: ${_e(d.get('price', '—'))}  "
            f"EMA8: {_e(round(ind.get('ema8', 0), 2))}  "
            f"VWAP: {_e(round(ind.get('vwap', 0), 2))}  "
            f"RSI3: {_e(round(ind.get('rsi3', 0), 1))}\n"
            + (f"   ❌ 未通過：{fail_text}" if failed else "   ✅ 所有條件通過")
        )
    await update.message.reply_text(
        f"📋 <b>Safety Check — 最近 5 次決策</b>\n{'─' * 24}\n\n"
        + "\n\n".join(lines)
        + f"\n\n📄 完整日誌：safety-check-log.json",
        parse_mode="HTML"
    )


async def cmd_tax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    from safety_check import get_tax_summary
    s = get_tax_summary()
    if "error" in s:
        await update.message.reply_text(f"📊 {s['error']}")
        return
    await update.message.reply_text(
        f"🧾 <b>稅務交易記錄</b>\n{'─' * 24}\n\n"
        f"總決策次數：{s['total']}\n"
        f"✅ 實盤成交：{s['live']}\n"
        f"📋 模擬成交：{s['paper']}\n"
        f"🚫 安全攔截：{s['blocked']}\n\n"
        f"💰 實盤總成交額：HKD {s['total_volume_hkd']:,.2f}\n"
        f"💸 手續費（估計）：HKD {s['total_fees_hkd']:,.4f}\n\n"
        f"📄 完整 CSV：trades.csv（可直接用 Excel / Google Sheets 開啟）",
        parse_mode="HTML"
    )


async def cmd_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    if not context.args:
        await update.message.reply_text("用法：/price 0700\n      /price AAPL")
        return
    ticker = context.args[0].upper()
    await update.message.reply_text(f"🔍 查詢 <b>{ticker}</b> 中，請稍候...", parse_mode="HTML")
    loop = asyncio.get_event_loop()
    from price_data import get_price_data
    prices = await loop.run_in_executor(None, get_price_data, ticker)
    if not prices:
        await update.message.reply_text(f"❌ 無法取得 {ticker} 數據，請確認代碼正確")
        return
    ma50_icon  = "✅" if prices.get("above_ma50") else "❌"
    ma200_icon = "✅" if prices.get("above_ma200") else "❌"
    macd_icon  = "📈 金叉" if prices.get("macd_bullish") else "📉 死叉"
    rsi = prices.get("rsi", 0)
    try:
        rsi_val = float(rsi)
        rsi_icon = "🔥 過熱" if rsi_val > 70 else "🧊 超賣" if rsi_val < 30 else "✅ 正常"
    except:
        rsi_icon = ""
    from ai_analyst import _calc_signal_score
    sig = _calc_signal_score(prices, "HOLD")
    filled = "█" * sig + "░" * (10 - sig)
    sig_emoji = "🔥" if sig >= 8 else "✅" if sig >= 5 else "⚠️"
    await update.message.reply_text(
        f"📊 <b>{ticker} 即時行情</b>\n{'─' * 24}\n\n"
        f"💰 現價：<b>${_e(prices['current_price'])}</b>\n"
        f"📈 MA50：${_e(prices.get('ma50','—'))} {ma50_icon}  ｜  MA200：${_e(prices.get('ma200','—'))} {ma200_icon}\n\n"
        f"📉 RSI：<b>{_e(rsi)}</b> {rsi_icon}\n"
        f"⚡️ MACD：{macd_icon}\n"
        f"🎯 布林帶：${_e(prices.get('bb_lower','—'))} — ${_e(prices.get('bb_upper','—'))}\n\n"
        f"🛑 止損參考：${_e(prices.get('stop_loss','—'))}\n"
        f"🎯 目標一：${_e(prices.get('target1','—'))}  ｜  🚀 目標二：${_e(prices.get('target2','—'))}\n\n"
        f"{sig_emoji} 技術評分：<b>{sig}/10</b>  [{filled}]",
        parse_mode="HTML"
    )


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text("📰 抓取最新港股新聞，請稍候...")
    loop = asyncio.get_event_loop()
    from news_fetcher import fetch_daily_news
    news = await loop.run_in_executor(None, fetch_daily_news)

    # 過濾港股相關
    hk_keywords = [
        "hong kong", "hkex", "hang seng", "china", "beijing", "pboc",
        "tencent", "alibaba", "meituan", "jd.com", "baidu", "byd",
        "hsbc", "ping an", "港", "中國", "騰訊", "阿里", "恒生",
    ]
    hk_news = [n for n in news if any(kw in (n["title"] + n["summary"]).lower() for kw in hk_keywords)]
    top5 = hk_news[:5] if hk_news else news[:5]

    lines = []
    for i, n in enumerate(top5):
        lines.append(
            f"<b>{i+1}. {_e(n['title'])}</b>\n"
            f"   📰 {_e(n['source'])}  🕐 {_e(n['published'])}"
        )

    await update.message.reply_text(
        f"📰 <b>今日港股即時新聞</b>\n{'─' * 24}\n\n"
        + "\n\n".join(lines)
        + f"\n\n💡 輸入 /run 讓 AI 對所有新聞進行完整分析",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update):
        return
    await update.message.reply_text(
        "📖 <b>指令列表</b>\n\n"
        "📊 <b>分析</b>\n"
        "/run — 立即跑今日市場分析\n"
        "/analyse AAPL — 完整 5-Prompt Stack 分析\n"
        "/price 0700 — 即時查價 + 技術評分\n"
        "/news — 今日港股即時新聞 Top 5\n"
        "/backtest AAPL — 回測過去6個月\n\n"
        "💰 <b>交易（模擬紙上交易）</b>\n"
        "/balance — 查看模擬賬戶資金\n"
        "/status — 查看 Futu 持倉（港股 + 美股）\n"
        "/pnl — 查看持倉損益\n"
        "/close AAPL — 平倉（以市價）\n"
        "/close AAPL 189.50 — 平倉（指定價格）\n"
        "/size AAPL 189 176 — 計算倉位大小\n\n"
        "📈 <b>績效 & 配置</b>\n"
        "/benchmark — Portfolio vs S&P 500 對比\n"
        "/portfolio — 核心衛星配置分析 + 風險警報\n"
        "/sharpe — 夏普比率計算\n"
        "/fundamentals 0700 — PE / EPS / ROE / 業績日\n\n"
        "📋 <b>名單管理</b>\n"
        "/watchlist — 查看監察名單\n"
        "/add TSLA — 加入股票\n"
        "/remove TSLA — 移除股票\n\n"
        "🏃 <b>日內交易</b>\n"
        "/daytrade — 查看日內交易狀態\n"
        "/daytrade on/off — 開啟/停止\n\n"
        "📓 <b>學習系統</b>\n"
        "/journal — 交易日誌 + 各信號勝率\n"
        "/lessons — AI 提取的教訓庫\n\n"
        "🔐 <b>Safety Check</b>\n"
        "/safety — 查看最近 5 次入場決策及通過/攔截原因\n"
        "/tax — 稅務交易記錄摘要\n\n"
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
            f"建議立即出場！輸入 /close {trade['ticker']} 平倉"
        ),
        parse_mode="HTML"
    )

async def send_preopen_ranking(rankings: list, southbound_flow: float = 0.0):
    if not rankings:
        return
    bot  = Bot(token=BOT_TOKEN)
    lines = []
    for i, r in enumerate(rankings[:10]):
        gap    = r["gap_pct"]
        emoji  = "🟢" if gap > 0 else "🔴" if gap < 0 else "⚪️"
        star   = " ⭐️" if i < 3 else ""
        lines.append(
            f"{emoji} <b>{r['code'].replace('HK.','')}</b>{star}  "
            f"${r['price']:.2f}  ({'+' if gap > 0 else ''}{gap:.2f}%)"
        )
    if southbound_flow > 500:
        sb_line = f"🔴 Southbound NET BUY <b>+{southbound_flow:,.0f}M HKD</b> — 北水大幅流入，偏好做多"
    elif southbound_flow < -500:
        sb_line = f"🔵 Southbound NET SELL <b>{southbound_flow:,.0f}M HKD</b> — 北水撤退，偏好做空"
    else:
        sb_line = f"⚪️ Southbound flow neutral ({southbound_flow:+,.0f}M HKD)"
    text = (
        f"🔭 <b>開市前選股排名</b>\n"
        f"{'─' * 24}\n"
        f"{sb_line}\n\n"
        f"按 gap 幅度排序，⭐️ 係今日重點關注\n\n"
        + "\n".join(lines)
        + f"\n\n📌 30分鐘ORB + VWAP RSI(2) 信號今日啟動"
    )
    await bot.send_message(chat_id=YOUR_CHAT_ID, text=text, parse_mode="HTML")


async def send_nightly_summary():
    from datetime import datetime
    import day_trader
    from trade_journal import get_stats

    bot     = Bot(token=BOT_TOKEN)
    weekly  = day_trader.get_weekly_state()
    positions = day_trader.get_open_positions()
    stats   = get_stats()

    pnl      = weekly["realized_pnl_hkd"]
    pct      = weekly["pct_of_pool"]
    target   = weekly["weekly_target_hkd"]
    fault    = weekly["fault_tolerance_hkd"]
    progress = min(100, round(pnl / target * 100)) if target > 0 and pnl > 0 else 0
    bar      = "█" * (progress // 10) + "░" * (10 - progress // 10)
    pnl_e    = "🟢" if pnl >= 0 else "🔴"

    pos_text = ""
    if positions:
        lines = [f"• {p['code']} x{p['qty']}  入:{p['entry']}  止:{p['stop']}" for p in positions]
        pos_text = (
            f"\n\n⚠️ <b>仍有 {len(positions)} 個未平倉位！</b>\n"
            + "\n".join(lines)
            + "\n請確認收市前已處理。"
        )

    win_e  = "🟢" if stats["win_rate"] >= 50 else "🔴"
    total_e = "🟢" if stats["total_pnl"] > 0 else "🔴"

    text = (
        f"🌙 <b>每晚總結 — {datetime.now().strftime('%Y-%m-%d')}</b>\n"
        f"{'─' * 26}\n\n"
        f"📊 <b>本週日內交易</b>\n"
        f"累計損益：{pnl_e} <b>HKD {pnl:+,.0f}</b>  ({pct:+.1f}%)\n"
        f"目標進度：[{bar}] {progress}%\n"
        f"週目標：HKD {target:,}　容錯上限：-HKD {fault:,}\n\n"
        f"📓 <b>交易日誌</b>\n"
        f"總筆數：{stats['trades']}　"
        f"{win_e} 勝率：{stats['win_rate']}%　"
        f"{total_e} 總損益：{'+' if stats['total_pnl'] > 0 else ''}{stats['total_pnl']}%"
        f"{pos_text}\n\n"
        f"💤 好好休息，明天繼續！"
    )
    await bot.send_message(chat_id=YOUR_CHAT_ID, text=text, parse_mode="HTML")


async def send_weekly_report(report: dict):
    bot = Bot(token=BOT_TOKEN)
    trades = report.get("trades", [])
    total = report.get("total_pnl", 0)

    # Benchmark comparison
    bench_text = ""
    try:
        from benchmark import get_benchmark_report
        bench = get_benchmark_report()
        if "error" not in bench:
            alpha = bench["alpha"]
            beat_str = "🏆 跑贏大市" if bench["beating_market"] else "📉 落後大市"
            bench_text = (
                f"\n\n{'─' * 20}\n"
                f"📈 vs S&P 500：Portfolio {'+' if bench['portfolio_return'] > 0 else ''}{bench['portfolio_return']}%  vs  SPY {'+' if bench['spy_return'] > 0 else ''}{bench['spy_return']}%\n"
                f"Alpha：<b>{'+' if alpha > 0 else ''}{alpha}%</b>  {beat_str}"
            )
    except:
        pass

    if not trades:
        await bot.send_message(chat_id=YOUR_CHAT_ID, text=f"📊 本週無持倉記錄{bench_text}", parse_mode="HTML")
        return

    lines = []
    for t in trades:
        e = "🟢" if t["pnl_pct"] > 0 else "🔴"
        lines.append(f"{e} {t['ticker']}：{'+' if t['pnl_pct'] > 0 else ''}{t['pnl_pct']}%")
    total_e = "🟢" if total > 0 else "🔴"
    await bot.send_message(
        chat_id=YOUR_CHAT_ID,
        text=(
            f"📊 <b>每週績效報告</b>\n{'─' * 24}\n\n"
            + "\n".join(lines)
            + f"\n\n{'─' * 24}\n{total_e} 總損益：<b>{'+' if total > 0 else ''}{total}%</b>"
            + bench_text
        ),
        parse_mode="HTML"
    )

async def reaction_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reaction = update.message_reaction
    if not reaction or reaction.chat.id != YOUR_CHAT_ID:
        return
    if reaction.message_id != _approval_msg_id:
        return

    new_emojis = {r.emoji for r in reaction.new_reaction if hasattr(r, "emoji")}

    if "✅" in new_emojis:
        from futu_trader import execute_trades
        from trade_tracker import record_trade
        execute_trades(today_decisions, dry_run=False)
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
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text=f"✅ <b>交易已記錄（Reaction 確認）</b>\n\n{text}\n\n輸入 /pnl 查看持倉損益",
            parse_mode="HTML",
        )

    elif "❌" in new_emojis:
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(
            chat_id=YOUR_CHAT_ID,
            text="❌ 今日交易已取消（Reaction 拒絕），明天見。",
        )


def run_telegram_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageReactionHandler(reaction_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("close", cmd_close))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("benchmark", cmd_benchmark))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("size", cmd_size))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("lessons", cmd_lessons))
    app.add_handler(CommandHandler("daytrade", cmd_daytrade))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("sharpe", cmd_sharpe))
    app.add_handler(CommandHandler("fundamentals", cmd_fundamentals))
    app.add_handler(CommandHandler("safety", cmd_safety))
    app.add_handler(CommandHandler("tax", cmd_tax))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("help", cmd_help))
    print("✅ Telegram Bot 已啟動")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
