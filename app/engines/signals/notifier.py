"""SignalNotifier — отправка сигналов в Telegram.

Логика (из STAGE_8_PLAN):
- WEAK   → не шлём (слишком слабый сигнал, спам не нужен)
- MEDIUM → 🟡 (предупреждение)
- STRONG → 🟢 BUY / 🔴 SELL (полноценный сигнал)

Использует существующий transport — app.telegram.notifier.send_notification().
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from app.engines.signals.models import Signal, SignalAction, SignalStrength
from app.telegram.notifier import send_notification

logger = logging.getLogger(__name__)


# ============================================================
# Эмодзи для форматирования
# ============================================================

_ACTION_EMOJI_STRONG = {
    SignalAction.BUY: "🟢",
    SignalAction.SELL: "🔴",
}


# Тип для inject'а transport-функции (для тестов — заменяем на mock)
SendNotificationFn = Callable[[str], Awaitable[bool]]


class SignalNotifier:
    """Форматирует сигнал и отправляет в Telegram.

    WEAK   → silently dropped
    MEDIUM → 🟡 одиночное предупреждение
    STRONG → 🟢 BUY / 🔴 SELL
    """

    def __init__(
        self,
        send_fn: SendNotificationFn = send_notification,
    ) -> None:
        """
        send_fn: транспорт-функция (по умолчанию — app.telegram.notifier.send_notification).
                 В тестах можно передать AsyncMock.
        """
        self._send_fn = send_fn

    # ============================================================
    # Публичный API
    # ============================================================

    async def notify(self, signal: Signal) -> bool:
        """Уведомить о сигнале.

        Возвращает:
            True  — сигнал отправлен в Telegram
            False — WEAK (пропускаем) ИЛИ transport вернул False (ошибка)
        """
        # WEAK сигналы — не шлём
        if signal.strength == SignalStrength.WEAK:
            logger.debug(
                "WEAK сигнал — Telegram не уведомляем",
                extra={
                    "symbol": signal.symbol,
                    "action": signal.action.value,
                    "score": signal.score,
                },
            )
            return False

        # Форматируем и отправляем
        text = self._format(signal)
        sent = await self._send_fn(text)

        if sent:
            logger.info(
                "Сигнал отправлен в Telegram",
                extra={
                    "symbol": signal.symbol,
                    "action": signal.action.value,
                    "strength": signal.strength.value,
                    "score": signal.score,
                },
            )
        else:
            logger.warning(
                "Telegram transport вернул False",
                extra={"symbol": signal.symbol, "action": signal.action.value},
            )

        return sent

    # ============================================================
    # Форматирование
    # ============================================================

    def _format(self, signal: Signal) -> str:
        """Форматирует Signal в многострочный текст для Telegram."""
        # Префикс с эмодзи
        if signal.strength == SignalStrength.STRONG:
            emoji = _ACTION_EMOJI_STRONG[signal.action]
            tag = f"[STRONG {signal.action.value}]"
        else:
            # MEDIUM
            emoji = "🟡"
            tag = f"[MEDIUM {signal.action.value}]"

        # Основные строки
        lines = [
            f"{emoji} {tag} {signal.symbol}",
            f"strength: {signal.strength.value}",
            f"score: {signal.score:.2f}",
        ]

        # Список сработавших правил
        if signal.reasons:
            lines.append(f"rules: {', '.join(signal.reasons)}")

        # Опциональный комментарий
        if signal.note:
            lines.append(f"note: {signal.note}")

        return "\n".join(lines)d