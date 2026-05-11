"""
Тесты Telegram Notifier — Stage 4.

Используем monkeypatch для подмены:
- settings.telegram_bot_token / telegram_chat_id (включение/выключение Telegram)
- telegram.Bot (мокаем Bot чтобы не делать реальных HTTP запросов)

Принципы:
- pytestmark = pytest.mark.unit — изолированные unit-тесты
- Никаких реальных HTTP вызовов к api.telegram.org
- Проверяем форматирование сообщений (контракт)
- Проверяем что без токена функции не падают
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.telegram.notifier import (
    _format_duration,
    notify_error,
    notify_trade_closed,
    notify_trade_opened,
    send_notification,
)

pytestmark = pytest.mark.unit


# ============================================================
# Helpers
# ============================================================

class _FakeSettings:
    """Подменяет get_settings() — управляем токеном из теста."""

    def __init__(self, token: str = "", chat_id: str = ""):
        self._token = token
        self.telegram_chat_id = chat_id

    @property
    def telegram_bot_token(self):
        # имитируем SecretStr — нужен .get_secret_value()
        wrapper = MagicMock()
        wrapper.get_secret_value.return_value = self._token
        return wrapper


def _patch_settings(monkeypatch, token: str = "", chat_id: str = "") -> None:
    """Подменить get_settings() внутри notifier."""
    fake = _FakeSettings(token=token, chat_id=chat_id)
    monkeypatch.setattr("app.telegram.notifier.get_settings", lambda: fake)


def _patch_bot(monkeypatch) -> MagicMock:
    """
    Подменить telegram.Bot — возвращает MagicMock с AsyncMock send_message.
    Возвращает мок-инстанс чтобы тест мог проверить вызовы.
    """
    bot_instance = MagicMock()
    bot_instance.send_message = AsyncMock()
    bot_class = MagicMock(return_value=bot_instance)
    monkeypatch.setattr("app.telegram.notifier.Bot", bot_class)
    return bot_instance


# ============================================================
# send_notification: базовое поведение
# ============================================================

class TestSendNotification:
    async def test_returns_false_when_token_empty(self, monkeypatch):
        """Без токена — функция молча возвращает False, не кидает."""
        _patch_settings(monkeypatch, token="", chat_id="12345")
        result = await send_notification("test message")
        assert result is False

    async def test_returns_false_when_chat_id_empty(self, monkeypatch):
        """Без chat_id — тоже False."""
        _patch_settings(monkeypatch, token="some-token", chat_id="")
        result = await send_notification("test message")
        assert result is False

    async def test_sends_message_when_configured(self, monkeypatch):
        """С токеном и chat_id — вызывает Bot.send_message."""
        _patch_settings(monkeypatch, token="real-token", chat_id="42")
        bot = _patch_bot(monkeypatch)

        result = await send_notification("hello")

        assert result is True
        bot.send_message.assert_awaited_once_with(chat_id="42", text="hello")

    async def test_returns_false_on_telegram_error(self, monkeypatch):
        """Если Telegram API кидает — функция возвращает False, не падает."""
        from telegram.error import TelegramError

        _patch_settings(monkeypatch, token="real-token", chat_id="42")
        bot = _patch_bot(monkeypatch)
        bot.send_message.side_effect = TelegramError("rate limited")

        result = await send_notification("hello")
        assert result is False


# ============================================================
# notify_trade_opened: форматирование
# ============================================================

class TestNotifyTradeOpened:
    async def test_formats_message_with_all_fields(self, monkeypatch):
        """Полный набор полей — должна сложиться многострочное сообщение."""
        _patch_settings(monkeypatch, token="t", chat_id="42")
        bot = _patch_bot(monkeypatch)

        await notify_trade_opened(
            symbol="BTCUSDT",
            side="Buy",
            qty=Decimal("0.005"),
            entry_price=Decimal("100000"),
            stop_loss=Decimal("99000"),
            take_profit=Decimal("102000"),
            leverage=5,
            ai_score=85.0,
            order_id="abc123",
        )

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        text = kwargs["text"]
        assert "[OPEN]" in text
        assert "BTCUSDT" in text
        assert "Buy" in text
        assert "0.005" in text
        assert "100000" in text
        assert "99000" in text
        assert "102000" in text
        assert "5x" in text
        assert "85.0" in text
        assert "abc123" in text
        # процентные дистанции должны быть посчитаны
        assert "-1.00%" in text   # SL = -1% от entry
        assert "+2.00%" in text   # TP = +2% от entry

    async def test_skips_optional_fields(self, monkeypatch):
        """Без ai_score и order_id — сообщение всё равно строится."""
        _patch_settings(monkeypatch, token="t", chat_id="42")
        bot = _patch_bot(monkeypatch)

        await notify_trade_opened(
            symbol="ETHUSDT",
            side="Sell",
            qty=Decimal("0.1"),
            entry_price=Decimal("3000"),
            stop_loss=Decimal("3030"),
            take_profit=Decimal("2940"),
            leverage=3,
        )

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        text = kwargs["text"]
        assert "ETHUSDT" in text
        assert "Sell" in text
        assert "score:" not in text
        assert "order:" not in text


# ============================================================
# notify_trade_closed: форматирование
# ============================================================

class TestNotifyTradeClosed:
    async def test_win_long_position(self, monkeypatch):
        """LONG с прибылью — PnL положительный, проценты считаются от entry."""
        _patch_settings(monkeypatch, token="t", chat_id="42")
        bot = _patch_bot(monkeypatch)

        await notify_trade_closed(
            symbol="BTCUSDT",
            side="Buy",
            entry_price=Decimal("100000"),
            exit_price=Decimal("102000"),
            pnl=Decimal("10"),
            result="win",
            duration_sec=3725,  # 1h 2m
            reason="take_profit_hit",
        )

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        text = kwargs["text"]
        assert "[CLOSE win]" in text
        assert "BTCUSDT" in text
        assert "+10" in text
        assert "+2.00%" in text
        assert "1h 2m" in text
        assert "take_profit_hit" in text

    async def test_loss_short_position(self, monkeypatch):
        """SHORT с убытком — PnL% инвертируется (цена выросла = плохо для шорта)."""
        _patch_settings(monkeypatch, token="t", chat_id="42")
        bot = _patch_bot(monkeypatch)

        await notify_trade_closed(
            symbol="ETHUSDT",
            side="Sell",
            entry_price=Decimal("3000"),
            exit_price=Decimal("3030"),
            pnl=Decimal("-5"),
            result="loss",
        )

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        text = kwargs["text"]
        assert "[CLOSE loss]" in text
        assert "-5" in text
        # для шорта exit > entry → PnL% отрицательный (-1%)
        assert "-1.00%" in text


# ============================================================
# notify_error: форматирование
# ============================================================

class TestNotifyError:
    async def test_basic_error(self, monkeypatch):
        _patch_settings(monkeypatch, token="t", chat_id="42")
        bot = _patch_bot(monkeypatch)

        await notify_error(
            action="open_position",
            error="RiskCheckFailed(kill_switch_enabled)",
            symbol="BTCUSDT",
            details="score=85.0",
        )

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        text = kwargs["text"]
        assert "[ERR]" in text
        assert "open_position failed" in text
        assert "BTCUSDT" in text
        assert "RiskCheckFailed" in text
        assert "score=85.0" in text

    async def test_minimal_error(self, monkeypatch):
        """Без symbol и details — всё равно работает."""
        _patch_settings(monkeypatch, token="t", chat_id="42")
        bot = _patch_bot(monkeypatch)

        await notify_error(action="emergency_close", error="timeout")

        bot.send_message.assert_awaited_once()
        _, kwargs = bot.send_message.call_args
        text = kwargs["text"]
        assert "[ERR] emergency_close failed" in text
        assert "timeout" in text


# ============================================================
# _format_duration: утилита
# ============================================================

class TestFormatDuration:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"),
        (45, "45s"),
        (59, "59s"),
        (60, "1m 0s"),
        (125, "2m 5s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (3725, "1h 2m"),
        (86399, "23h 59m"),
        (86400, "1d 0h"),
        (90000, "1d 1h"),
    ])
    def test_format(self, seconds: int, expected: str):
        assert _format_duration(seconds) == expected