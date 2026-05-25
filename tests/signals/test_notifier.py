"""Тесты SignalNotifier — форматирование и отправка в Telegram.

Используем AsyncMock вместо реального транспорта.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.engines.signals.models import Signal, SignalAction, SignalStrength
from app.engines.signals.notifier import SignalNotifier


# ============================================================
# Хелпер
# ============================================================

def _make_signal(
    action: SignalAction,
    strength: SignalStrength,
    score: float = 0.5,
    reasons=None,
    symbol: str = "BTCUSDT",
) -> Signal:
    """Фабрика тестовых сигналов."""
    return Signal(
        symbol=symbol,
        timestamp_ms=1_700_000_000_000,
        action=action,
        strength=strength,
        score=score,
        reasons=reasons or ["test_rule"],
        snapshot={"obi_top10": 0.8},
    )


# ============================================================
# Тесты
# ============================================================

@pytest.mark.asyncio
async def test_weak_signal_not_sent():
    """WEAK сигнал не должен передаваться в transport — notify() возвращает False."""
    transport = AsyncMock(return_value=True)
    notifier = SignalNotifier(send_fn=transport)

    signal = _make_signal(
        action=SignalAction.BUY,
        strength=SignalStrength.WEAK,
        score=0.25,
    )

    result = await notifier.notify(signal)

    assert result is False
    # Transport НЕ должен быть вызван — мы дропнули WEAK тихо
    transport.assert_not_called()


@pytest.mark.asyncio
async def test_medium_signal_sent_with_yellow_emoji():
    """MEDIUM сигнал передаётся в transport, в тексте 🟡 и [MEDIUM ACTION]."""
    transport = AsyncMock(return_value=True)
    notifier = SignalNotifier(send_fn=transport)

    signal = _make_signal(
        action=SignalAction.SELL,
        strength=SignalStrength.MEDIUM,
        score=0.5,
        reasons=["cvd_trend", "exhaustion"],
    )

    result = await notifier.notify(signal)

    assert result is True
    transport.assert_called_once()

    sent_text = transport.call_args.args[0]
    assert "🟡" in sent_text
    assert "[MEDIUM SELL]" in sent_text
    assert "BTCUSDT" in sent_text
    assert "score: 0.50" in sent_text
    assert "cvd_trend" in sent_text
    assert "exhaustion" in sent_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,expected_emoji,expected_tag",
    [
        (SignalAction.BUY, "🟢", "[STRONG BUY]"),
        (SignalAction.SELL, "🔴", "[STRONG SELL]"),
    ],
)
async def test_strong_signal_sent_with_action_emoji(
    action, expected_emoji, expected_tag
):
    """STRONG сигнал: BUY → 🟢, SELL → 🔴, теги соответствуют."""
    transport = AsyncMock(return_value=True)
    notifier = SignalNotifier(send_fn=transport)

    signal = _make_signal(
        action=action,
        strength=SignalStrength.STRONG,
        score=0.85,
        reasons=["orderbook_imbalance", "cvd_trend", "large_trades"],
    )

    result = await notifier.notify(signal)

    assert result is True
    transport.assert_called_once()

    sent_text = transport.call_args.args[0]
    assert expected_emoji in sent_text
    assert expected_tag in sent_text
    assert "strength: STRONG" in sent_text
    assert "score: 0.85" in sent_text