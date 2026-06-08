"""
Telegram Bot Commands — Stage 13
Commands: /status /settings /control /ai_report
"""

import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings
from app.api.settings import (
    build_status_response,
    build_settings_response,
    update_bot_state,
)
from app.engines.self_learning.report_service import build_decision_report

logger = logging.getLogger(__name__)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получить статус бота"""
    try:
        data = await build_status_response()
        trading = "ON ✅" if data['trading_enabled'] else "OFF ⛔"
        paused = "Yes ⏸️" if data['bot_paused'] else "No"
        balance = data['balance'] or 'N/A'
        positions = data['open_positions']
        trades = data['total_trades']
        wr = f"{data['win_rate']:.1f}%" if data['win_rate'] else 'N/A'
        ai_dec = data['ai_decisions']
        sl_ready = "Yes ✓" if data['self_learning_ready'] else "No"
        
        msg = f"*Bot Status* 🤖\n\n*State:*\n• Trading: {trading}\n• Paused: {paused}\n\n*Financials:*\n• Balance: ${balance}\n• Open Positions: {positions}\n\n*History:*\n• Total Trades: {trades}\n• Win Rate: {wr}\n\n*AI:*\n• Decisions: {ai_dec}\n• Self-Learning: {sl_ready}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.exception("cmd_status failed")
        await update.message.reply_text(f"Error: {e}")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Получить параметры"""
    try:
        data = await build_settings_response()
        trading = "ON ✅" if data['trading_enabled'] else "OFF ⛔"
        paused = "Yes" if data['bot_paused'] else "No"
        lev = data['default_leverage']
        risk = data['risk_level']
        score = data['min_final_score_trade']
        max_risk = data['max_risk_per_trade'] * 100
        max_loss = data['max_daily_loss'] * 100
        max_consec = data['max_consecutive_losses']
        kill = "ON 🔴" if data['kill_switch_enabled'] else "OFF"
        
        msg = f"*Bot Settings* ⚙️\n\n*Control:*\n• Trading: {trading}\n• Paused: {paused}\n\n*Parameters:*\n• Leverage: {lev}x\n• Risk: {risk}\n• Min Score: {score}\n• Max Risk: {max_risk:.1f}%\n• Max Loss: {max_loss:.1f}%\n• Max Losses: {max_consec}\n• Kill Switch: {kill}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.exception("cmd_settings failed")
        await update.message.reply_text(f"Error: {e}")


async def cmd_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Управление ботом"""
    try:
        if not context.args:
            msg = "*Bot Control* 🎮\n\nUsage:\n/control on\n/control off\n/control leverage 5\n/control risk low"
            await update.message.reply_text(msg, parse_mode='Markdown')
            return
        
        action = context.args[0].lower()
        value = context.args[1] if len(context.args) > 1 else None
        
        if action == "on":
            await update_bot_state({"trading_enabled": True})
            await update.message.reply_text("✅ Trading enabled!")
        elif action == "off":
            await update_bot_state({"trading_enabled": False})
            await update.message.reply_text("⛔ Trading disabled!")
        elif action == "leverage":
            if not value:
                await update.message.reply_text("Error: specify leverage")
                return
            lev = int(value)
            if lev < 1 or lev > 125:
                await update.message.reply_text("Error: 1-125")
                return
            await update_bot_state({"default_leverage": lev})
            await update.message.reply_text(f"✅ Leverage: {lev}x")
        elif action == "risk":
            if not value or value.lower() not in ["low", "medium", "high"]:
                await update.message.reply_text("Error: low|medium|high")
                return
            await update_bot_state({"risk_level": value.lower()})
            await update.message.reply_text(f"✅ Risk: {value.lower()}")
        else:
            await update.message.reply_text(f"Unknown: {action}")
    except Exception as e:
        logger.exception("cmd_control failed")
        await update.message.reply_text(f"Error: {e}")


async def cmd_ai_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Self-Learning отчёт"""
    try:
        report = await build_decision_report(limit=200)
        
        if report.get("status") == "no_data":
            await update.message.reply_text("No AI decisions yet")
            return
        
        total = report.get("total_decisions", 0)
        counts = report.get("counts", {})
        by_dir = counts.get("by_direction", {})
        insights = report.get("insights", [])[:3]
        
        msg = f"*Self-Learning Report* 🧠\n\n*Summary:*\n• Decisions: {total}\n• LONG/SHORT: {by_dir.get('LONG', 0)}/{by_dir.get('SHORT', 0)}\n\n*Insights:*\n"
        for i, insight in enumerate(insights, 1):
            msg += f"\n{i}. {insight}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.exception("cmd_ai_report failed")
        await update.message.reply_text(f"Error: {e}")




async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backtest results"""
    try:
        from app.engines.backtest.engine import run_backtest
        results = await run_backtest(limit=1000)
        
        if results.get("status") != "ok":
            await update.message.reply_text("Backtest unavailable")
            return
        
        metrics = results.get("metrics", {})
        msg = f"*Backtest Results* 📊\n\n*Summary:*\n• Trades: {metrics.get('total_trades', 0)}\n• Wins: {metrics.get('wins', 0)}\n• Losses: {metrics.get('losses', 0)}\n• Win Rate: {metrics.get('win_rate', 0):.1f}%\n\n*Performance:*\n• Profit Factor: {metrics.get('profit_factor', 0):.2f}\n• Total Return: {metrics.get('total_return', 0):.1f}%\n• Max Drawdown: {metrics.get('max_drawdown', 0):.2f}%\n• Sharpe: {metrics.get('sharpe_ratio', 0):.2f}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.exception("cmd_backtest failed")
        await update.message.reply_text(f"Error: {e}")




async def cmd_open_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Открытые позиции"""
    try:
        from app.engines.executor.executor import TradeExecutor
        executor = TradeExecutor()
        result = await executor.get_open_positions()
        
        positions = result.get("positions", [])
        if not positions:
            await update.message.reply_text("No open positions")
            return
        
        msg = "*Open Positions* 📈\n\n"
        for pos in positions:
            pnl_emoji = "📈" if pos["pnl"] > 0 else "📉"
            msg += f"{pnl_emoji} {pos['symbol']} {pos['side']}\n• Size: {pos['size']}\n• Entry: ${pos['entry_price']:.2f}\n• Mark: ${pos['mark_price']:.2f}\n• P&L: ${pos['pnl']:.2f}\n\n"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.exception("cmd_open_trades failed")
        await update.message.reply_text(f"Error: {e}")




async def cmd_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Управление автоматической торговлей"""
    try:
        if not context.args:
            await update.message.reply_text("*Auto Trading*\n\n/auto_trading on — включить\n/auto_trading off — выключить", parse_mode='Markdown')
            return
        
        action = context.args[0].lower()
        if action == "on":
            await update_bot_state({"auto_trading_enabled": True})
            await update.message.reply_text("✅ Auto trading enabled!")
        elif action == "off":
            await update_bot_state({"auto_trading_enabled": False})
            await update.message.reply_text("⛔ Auto trading disabled!")
    except Exception as e:
        logger.exception("cmd_auto_trading failed")
        await update.message.reply_text(f"Error: {e}")


def get_telegram_bot():
    """Инициализация бота"""
    settings = get_settings()
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set")
        return None
    
    token = settings.telegram_bot_token.get_secret_value()
    app = Application.builder().token(token).build()
    
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("control", cmd_control))
    app.add_handler(CommandHandler("ai_report", cmd_ai_report))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("open_trades", cmd_open_trades))
    app.add_handler(CommandHandler("auto_trading", cmd_auto_trading))
    
    logger.info("Telegram bot initialized")
    return app


async def cmd_backtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backtest results"""
    try:
        from app.engines.backtest.engine import run_backtest
        results = await run_backtest(limit=1000)
        
        if results.get("status") != "ok":
            await update.message.reply_text("Backtest unavailable")
            return
        
        metrics = results.get("metrics", {})
        msg = f"*Backtest Results* 📊\n\n*Summary:*\n• Trades: {metrics.get('total_trades', 0)}\n• Wins: {metrics.get('wins', 0)}\n• Losses: {metrics.get('losses', 0)}\n• Win Rate: {metrics.get('win_rate', 0):.1f}%\n\n*Performance:*\n• Profit Factor: {metrics.get('profit_factor', 0):.2f}\n• Total Return: {metrics.get('total_return', 0):.1f}%\n• Max Drawdown: {metrics.get('max_drawdown', 0):.2f}%\n• Sharpe Ratio: {metrics.get('sharpe_ratio', 0):.2f}"
        
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.exception("cmd_backtest failed")
        await update.message.reply_text(f"Error: {e}")


# Добавляем handler
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("open_trades", cmd_open_trades))
    app.add_handler(CommandHandler("auto_trading", cmd_auto_trading))


async def cmd_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Управление автоматической торговлей: /auto_trading on|off"""
    try:
        if not context.args:
            msg = "*Auto Trading Control*\n\n/auto_trading on — включить\n/auto_trading off — выключить"
            await update.message.reply_text(msg, parse_mode='Markdown')
            return
        
        action = context.args[0].lower()
        
        if action == "on":
            await update_bot_state({"auto_trading_enabled": True})
            await update.message.reply_text("✅ Auto trading enabled!")
        elif action == "off":
            await update_bot_state({"auto_trading_enabled": False})
            await update.message.reply_text("⛔ Auto trading disabled!")
        else:
            await update.message.reply_text("Usage: /auto_trading on|off")
    except Exception as e:
        logger.exception("cmd_auto_trading failed")
        await update.message.reply_text(f"Error: {e}")
