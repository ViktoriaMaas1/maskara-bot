"""Тесты SignalGenerator — главного класса Signal Generator.

Mock'аем все зависимости:
- cooldown: фейковый CooldownGate (AsyncMock)
- notifier: фейковый SignalNotifier (AsyncMock)
- session_factory: фейковая фабрика, возвращает фейковую сессию

Так мы тестируем только логику combine + pipeline,
без реальной БД, Redis, Telegram.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.engines.order_flow.models import OrderFlowSnapshot
from app.engines.signals.generator import SignalGenerator
from app.engines.signals.models import SignalAction, SignalStrength


# ============================================================
# Фикстуры
# ============================================================

@pytest.fixture
def mock_cooldown():
    """Фейковый CooldownGate — по умолчанию всё разрешает."""
    gate = MagicMock()
    gate.is_allowed = AsyncMock(return_value=True)
    gate.mark_sent = AsyncMock(return_value=None)
    return gate


@pytest.fixture
def mock_notifier():
    """Фейковый SignalNotifier."""
    notifier = MagicMock()
    notifier.notify = AsyncMock(return_value=True)
    return notifier


@pytest.fixture
def mock_session_factory():
    """Фабрика, возвращающая async context manager с фейковой сессией.

    Сессия имеет .commit(), .add(), .flush(), .refresh() — все AsyncMock'и.
    """
    @asynccontextmanager
    async def _factory():
        session = MagicMock()
        session.commit = AsyncMock()
        session.add = MagicMock()
        session.flush = AsyncMock()
        session.refresh = AsyncMock()
        yield session

    return _factory


@pytest.fixture
def generator(mock_session_factory, mock_cooldown, mock_notifier):
    """Готовый SignalGenerator со всеми mock-зависимостями."""
    return SignalGenerator(
        session_factory=mock_session_factory,
        cooldown=mock_cooldown,
        notifier=mock_notifier,
    )


# ============================================================
# Фабрики snapshot'ов под разные сценарии
# ============================================================

def _make_snapshot(**overrides) -> OrderFlowSnapshot:
    """Базовый snapshot, нейтральные значения."""
    defaults = dict(
        symbol="BTCUSDT",
        timestamp_ms=1_700_000_000_000,
        data_available=True,
        delta_30s=0.0,
        delta_1m=0.0,
        delta_5m=0.0,
        cvd=0.0,
        obi_top5=0.0,
        obi_top10=0.0,
        obi_top20=0.0,
        tfi_30s=0.0,
        tfi_1m=0.0,
        tfi_5m=0.0,
        buy_aggression_1m=0.5,
        total_volume_1m=0.0,
        large_trade_count_1m=0,
        trades_count_1m=0,
        orderbook_age_ms=100,
    )
    defaults.update(overrides)
    return OrderFlowSnapshot(**defaults)


# ============================================================
# Тесты
# ============================================================

@pytest.mark.asyncio
async def test_no_rules_triggered_returns_none(generator, mock_cooldown, mock_notifier):
    """Нейтральный snapshot → ни одно правило не сработало → None."""
    snapshot = _make_snapshot()  # всё в нулях, ни одно правило не сработает

    result = await generator.process_snapshot(snapshot)

    assert result is None
    # Cooldown даже не проверяли, ничего не отправляли
    mock_cooldown.is_allowed.assert_not_called()
    mock_cooldown.mark_sent.assert_not_called()
    mock_notifier.notify.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_buy_and_sell_returns_none(
    generator, mock_cooldown, mock_notifier
):
    """OBI кричит BUY, CVD кричит SELL → конфликт → None."""
    # OBI bullish (BUY правило сработает)
    # CVD bearish (SELL правило сработает)
    snapshot = _make_snapshot(
        obi_top10=0.85,
        buy_aggression_1m=0.8,
        cvd=-3.0,
        delta_30s=-0.5,
    )

    result = await generator.process_snapshot(snapshot)

    assert result is None
    # Cooldown не должен был спрашиваться — мы дропнули раньше
    mock_cooldown.is_allowed.assert_not_called()
    mock_notifier.notify.assert_not_called()


@pytest.mark.asyncio
async def test_single_rule_returns_weak(generator, mock_cooldown, mock_notifier):
    """Сработало только 1 правило → WEAK сигнал, полный pipeline."""
    # Только rule_orderbook_imbalance сработает (OBI + aggression на BUY)
    snapshot = _make_snapshot(
        obi_top10=0.85,
        buy_aggression_1m=0.75,
    )

    result = await generator.process_snapshot(snapshot)

    assert result is not None
    assert result.action == SignalAction.BUY
    assert result.strength == SignalStrength.WEAK
    assert result.score == pytest.approx(0.25, abs=0.01)
    assert "orderbook_imbalance" in result.reasons

    # Cooldown проверили и поставили
    mock_cooldown.is_allowed.assert_awaited_once_with("BTCUSDT", "BUY")
    mock_cooldown.mark_sent.assert_awaited_once_with("BTCUSDT", "BUY")
    # Notifier вызван — он сам решит, слать или дропнуть WEAK
    mock_notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_three_rules_return_strong(generator, mock_cooldown, mock_notifier):
    """3 правила сработали в одну сторону → STRONG, score 0.75."""
    # Bullish сразу по нескольким направлениям:
    # 1) OBI imbalance: obi_top10 + aggression
    # 2) CVD trend: cvd > 1.0 + delta_30s > 0
    # 3) Large trades: large_trade_count_1m + tfi_1m > 0.5
    snapshot = _make_snapshot(
        obi_top10=0.85,
        buy_aggression_1m=0.8,
        cvd=2.5,
        delta_30s=0.4,
        large_trade_count_1m=7,
        tfi_1m=0.65,
    )

    result = await generator.process_snapshot(snapshot)

    assert result is not None
    assert result.action == SignalAction.BUY
    assert result.strength == SignalStrength.STRONG
    assert result.score == pytest.approx(0.75, abs=0.01)
    # Все 3 правила должны быть в reasons
    assert "orderbook_imbalance" in result.reasons
    assert "cvd_trend" in result.reasons
    assert "large_trades" in result.reasons

    mock_cooldown.mark_sent.assert_awaited_once()
    mock_notifier.notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_cooldown_blocks_signal(generator, mock_cooldown, mock_notifier):
    """Cooldown активен → сигнал не возвращается, save/notify не вызваны."""
    # Cooldown говорит "нельзя"
    mock_cooldown.is_allowed = AsyncMock(return_value=False)

    snapshot = _make_snapshot(
        obi_top10=0.85,
        buy_aggression_1m=0.8,
        cvd=2.5,
        delta_30s=0.4,
    )

    result = await generator.process_snapshot(snapshot)

    assert result is None
    # is_allowed спросили
    mock_cooldown.is_allowed.assert_awaited_once_with("BTCUSDT", "BUY")
    # А mark_sent НЕ дёргали — мы заблокировались до этого
    mock_cooldown.mark_sent.assert_not_called()
    # И notifier тоже не дёргали
    mock_notifier.notify.assert_not_called()