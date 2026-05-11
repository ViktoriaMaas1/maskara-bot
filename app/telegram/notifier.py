"""
Telegram Notifier — Stage 4

Односторонняя отправка уведомлений в Telegram. Не command bot (это Stage 13).

Архитектура:
- Используем python-telegram-bot для отправки сообщений
- Если telegram_bot_token или telegram_chat_id пустые — функции молча
  возвращаются (логируют warning). Это позволяет работать без Telegram
  в dev/test окружениях.
- Все функции async — работают в event loop FastAPI
- Ошибки сети/Telegram API логируются, но НЕ кидаются дальше —
  падение Telegram не должно валить торговлю

Используется ExecutionEngine:
- notify_trade_opened — после успешного открытия позиции
- notify_trade_closed — после закрытия позиции
- notify_error       — при любых ошибках Execution Engine
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from telegram import Bot
from telegram.error import TelegramError

from app.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================
# Низкоуровневая отправка
# ============================================================

async def send_notification(text: str) -> bool:
    """
    Отправить текст в настроенный Telegram-чат.

    Returns:
        True если сообщение отправлено
        False если Telegram не настроен (пустой токен/chat_id) или произошла ошибка

    НЕ кидает исключений — все ошибки логируются и проглатываются.
    Telegram-сбой не должен валить торговую логику.
    """
    settings = get_settings()
    token = settings.telegram_bot_token.get_secret_value()
    chat_id = settings.telegram_chat_id

    if not token or not chat_id:
        logger.warning(
            "Telegram not configured: token=%s chat_id=%s — skipping notification",
            "set" if token else "empty",
            "set" if chat_id else "empty",
        )
        return False

    try:
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)
        logger.debug("Telegram notification sent: %s", text[:80])
        return True
    except TelegramError as e:
        logger.error("Telegram API error: %s", e)
        return False
    except Exception as e:
        logger.exception("Unexpected error sending Telegram message: %s", e)
        return False


# ============================================================
# Форматированные уведомления для Execution Engine
# ============================================================

async def notify_trade_opened(
    *,
    symbol: str,
    side: str,
    qty: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    leverage: int,
    ai_score: Optional[float] = None,
    order_id: Optional[str] = None,
) -> bool:
    """
    Уведомление об открытии позиции.

    Пример сообщения:
        [OPEN] BTCUSDT Buy
        qty: 0.005
        entry: 100000
        SL: 99000 (-1.0%)
        TP: 102000 (+2.0%)
        leverage: 5x
        score: 85.0
        order: abc123
    """
    # Расчёт процентных дистанций до SL/TP (для удобства чтения)
    if entry_price > 0:
        sl_pct = (stop_loss - entry_price) / entry_price * 100
        tp_pct = (take_profit - entry_price) / entry_price * 100
    else:
        sl_pct = Decimal("0")
        tp_pct = Decimal("0")

    lines = [
        f"[OPEN] {symbol} {side}",
        f"qty: {qty}",
        f"entry: {entry_price}",
        f"SL: {stop_loss} ({sl_pct:+.2f}%)",
        f"TP: {take_profit} ({tp_pct:+.2f}%)",
        f"leverage: {leverage}x",
    ]
    if ai_score is not None:
        lines.append(f"score: {ai_score:.1f}")
    if order_id:
        lines.append(f"order: {order_id}")

    return await send_notification("\n".join(lines))


async def notify_trade_closed(
    *,
    symbol: str,
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    pnl: Decimal,
    result: str,                    # "win" / "loss" / "breakeven"
    duration_sec: Optional[int] = None,
    reason: Optional[str] = None,
) -> bool:
    """
    Уведомление о закрытии позиции.

    Пример сообщения:
        [CLOSE win] BTCUSDT Buy
        entry: 100000
        exit: 102000
        PnL: +10.0 USDT (+2.00%)
        duration: 1h 23m
        reason: take_profit_hit
    """
    if entry_price > 0:
        pnl_pct = (exit_price - entry_price) / entry_price * 100
        if side.lower() in ("sell", "short"):
            pnl_pct = -pnl_pct
    else:
        pnl_pct = Decimal("0")

    lines = [
        f"[CLOSE {result}] {symbol} {side}",
        f"entry: {entry_price}",
        f"exit: {exit_price}",
        f"PnL: {pnl:+} USDT ({pnl_pct:+.2f}%)",
    ]
    if duration_sec is not None:
        lines.append(f"duration: {_format_duration(duration_sec)}")
    if reason:
        lines.append(f"reason: {reason}")

    return await send_notification("\n".join(lines))


async def notify_error(
    *,
    action: str,                    # что пытались сделать
    error: str,                     # описание ошибки
    symbol: Optional[str] = None,
    details: Optional[str] = None,
) -> bool:
    """
    Уведомление об ошибке Execution Engine.

    Пример сообщения:
        [ERR] open_position failed
        symbol: BTCUSDT
        error: RiskCheckFailed(consecutive_losses(3>=3))
        details: AI score was 85.0
    """
    lines = [f"[ERR] {action} failed"]
    if symbol:
        lines.append(f"symbol: {symbol}")
    lines.append(f"error: {error}")
    if details:
        lines.append(f"details: {details}")

    return await send_notification("\n".join(lines))


# ============================================================
# Утилиты
# ============================================================

def _format_duration(seconds: int) -> str:
    """
    Форматирует секунды в человекочитаемый вид.

    Примеры:
        45     -> "45s"
        125    -> "2m 5s"
        3725   -> "1h 2m"
        90000  -> "1d 1h"
    """
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m}m"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d {h}h"