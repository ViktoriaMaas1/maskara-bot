"""Фикстуры OrderFlowSnapshot'ов для тестов Signal Generator.

Каждая фикстура — это готовый снимок рынка под конкретный сценарий:
сильный bullish дисбаланс, медвежий CVD-тренд, кит покупает, истощение и т.д.
Используется в test_rules.py, test_generator.py и т.д.
"""

from __future__ import annotations

import pytest

from app.engines.order_flow.models import OrderFlowSnapshot


# ============================================================
# Базовая фабрика
# ============================================================

def _make_snapshot(**overrides) -> OrderFlowSnapshot:
    """Базовый snapshot с нейтральными значениями.

    Все поля по умолчанию = 0.0 (или 0). Через **overrides можно
    переопределить только те поля, что важны для конкретного сценария.
    """
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
# Нейтральные / служебные snapshot'ы
# ============================================================

@pytest.fixture
def neutral_snapshot() -> OrderFlowSnapshot:
    """Пустой нейтральный рынок. Ни одно правило не должно сработать."""
    return _make_snapshot()


@pytest.fixture
def no_data_snapshot() -> OrderFlowSnapshot:
    """data_available=False — все правила обязаны вернуть None."""
    return _make_snapshot(data_available=False)


# ============================================================
# Правило 1: orderbook imbalance + aggression
# ============================================================

@pytest.fixture
def bullish_orderbook_snapshot() -> OrderFlowSnapshot:
    """Сильный OBI вверх + агрессивные покупатели → BUY по правилу 1."""
    return _make_snapshot(
        obi_top10=0.8,
        buy_aggression_1m=0.75,
    )


@pytest.fixture
def bearish_orderbook_snapshot() -> OrderFlowSnapshot:
    """Сильный OBI вниз + агрессивные продавцы → SELL по правилу 1."""
    return _make_snapshot(
        obi_top10=-0.8,
        buy_aggression_1m=0.25,
    )


@pytest.fixture
def weak_orderbook_snapshot() -> OrderFlowSnapshot:
    """OBI слабый — правило 1 не должно сработать."""
    return _make_snapshot(
        obi_top10=0.3,
        buy_aggression_1m=0.55,
    )


# ============================================================
# Правило 2: CVD trend
# ============================================================

@pytest.fixture
def bullish_cvd_snapshot() -> OrderFlowSnapshot:
    """Сильный накопленный CVD вверх + текущий импульс → BUY по правилу 2."""
    return _make_snapshot(
        cvd=2.5,
        delta_30s=0.4,
    )


@pytest.fixture
def bearish_cvd_snapshot() -> OrderFlowSnapshot:
    """Сильный накопленный CVD вниз + текущий импульс → SELL по правилу 2."""
    return _make_snapshot(
        cvd=-2.5,
        delta_30s=-0.4,
    )


@pytest.fixture
def cvd_no_momentum_snapshot() -> OrderFlowSnapshot:
    """CVD большой, но delta_30s = 0 → правило 2 не должно сработать."""
    return _make_snapshot(
        cvd=2.5,
        delta_30s=0.0,
    )


# ============================================================
# Правило 3: large trades (кит вошёл)
# ============================================================

@pytest.fixture
def whale_buy_snapshot() -> OrderFlowSnapshot:
    """Много крупных сделок + положительный TFI → BUY по правилу 3."""
    return _make_snapshot(
        large_trade_count_1m=7,
        tfi_1m=0.65,
    )


@pytest.fixture
def whale_sell_snapshot() -> OrderFlowSnapshot:
    """Много крупных сделок + отрицательный TFI → SELL по правилу 3."""
    return _make_snapshot(
        large_trade_count_1m=7,
        tfi_1m=-0.65,
    )


@pytest.fixture
def whale_neutral_tfi_snapshot() -> OrderFlowSnapshot:
    """Киты есть, но TFI близок к нулю → правило 3 не сработает."""
    return _make_snapshot(
        large_trade_count_1m=7,
        tfi_1m=0.1,
    )


@pytest.fixture
def few_large_trades_snapshot() -> OrderFlowSnapshot:
    """Крупных сделок мало (< порога) → правило 3 не сработает."""
    return _make_snapshot(
        large_trade_count_1m=2,
        tfi_1m=0.9,
    )


# ============================================================
# Правило 4: exhaustion
# ============================================================

@pytest.fixture
def exhaustion_buy_snapshot() -> OrderFlowSnapshot:
    """Медведи истощены (tfi_5m << 0) + краткосрочный разворот → BUY."""
    return _make_snapshot(
        tfi_5m=-0.75,
        tfi_30s=0.4,
    )


@pytest.fixture
def exhaustion_sell_snapshot() -> OrderFlowSnapshot:
    """Быки истощены (tfi_5m >> 0) + краткосрочный разворот → SELL."""
    return _make_snapshot(
        tfi_5m=0.75,
        tfi_30s=-0.4,
    )


@pytest.fixture
def exhaustion_no_reversal_snapshot() -> OrderFlowSnapshot:
    """Истощение есть, но разворота нет → правило 4 не сработает."""
    return _make_snapshot(
        tfi_5m=-0.75,
        tfi_30s=0.1,
    )


# ============================================================
# Комбинированные сценарии (для будущих тестов SignalGenerator)
# ============================================================

@pytest.fixture
def strong_bullish_all_rules_snapshot() -> OrderFlowSnapshot:
    """Все 4 правила должны сработать на BUY — STRONG signal."""
    return _make_snapshot(
        obi_top10=0.85,
        buy_aggression_1m=0.8,
        cvd=3.0,
        delta_30s=0.5,
        large_trade_count_1m=8,
        tfi_1m=0.7,
        tfi_5m=-0.7,
        tfi_30s=0.45,
    )


@pytest.fixture
def conflicting_signals_snapshot() -> OrderFlowSnapshot:
    """Часть правил кричит BUY, часть SELL → конфликт, Generator вернёт None.

    OBI bullish, но CVD bearish.
    """
    return _make_snapshot(
        obi_top10=0.85,
        buy_aggression_1m=0.8,
        cvd=-3.0,
        delta_30s=-0.5,
    )