"""Тесты для 4 правил Signal Generator.

Каждое правило проверяется в 3 сценариях: BUY, SELL, None.
Итого: 4 × 3 = 12 тестов.

Все фикстуры snapshot'ов — в conftest.py.
"""

from __future__ import annotations

from app.engines.signals.models import Signal, SignalAction
from app.engines.signals.rules import (
    rule_cvd_trend,
    rule_exhaustion,
    rule_large_trades,
    rule_orderbook_imbalance,
)


# ============================================================
# Правило 1: rule_orderbook_imbalance
# ============================================================

def test_orderbook_imbalance_buy(bullish_orderbook_snapshot):
    """OBI сильный вверх + агрессия покупателей → BUY."""
    signal = rule_orderbook_imbalance(bullish_orderbook_snapshot)

    assert signal is not None
    assert isinstance(signal, Signal)
    assert signal.action == SignalAction.BUY
    assert signal.symbol == "BTCUSDT"
    assert "orderbook_imbalance" in signal.reasons


def test_orderbook_imbalance_sell(bearish_orderbook_snapshot):
    """OBI сильный вниз + агрессия продавцов → SELL."""
    signal = rule_orderbook_imbalance(bearish_orderbook_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.SELL
    assert "orderbook_imbalance" in signal.reasons


def test_orderbook_imbalance_none(weak_orderbook_snapshot):
    """OBI слабый → правило не должно сработать."""
    signal = rule_orderbook_imbalance(weak_orderbook_snapshot)

    assert signal is None


# ============================================================
# Правило 2: rule_cvd_trend
# ============================================================

def test_cvd_trend_buy(bullish_cvd_snapshot):
    """CVD большой положительный + delta_30s > 0 → BUY."""
    signal = rule_cvd_trend(bullish_cvd_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert "cvd_trend" in signal.reasons


def test_cvd_trend_sell(bearish_cvd_snapshot):
    """CVD большой отрицательный + delta_30s < 0 → SELL."""
    signal = rule_cvd_trend(bearish_cvd_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.SELL
    assert "cvd_trend" in signal.reasons


def test_cvd_trend_none(cvd_no_momentum_snapshot):
    """CVD есть, но текущего импульса (delta_30s) нет → None."""
    signal = rule_cvd_trend(cvd_no_momentum_snapshot)

    assert signal is None


# ============================================================
# Правило 3: rule_large_trades
# ============================================================

def test_large_trades_buy(whale_buy_snapshot):
    """Много крупных сделок + tfi_1m положительный → BUY."""
    signal = rule_large_trades(whale_buy_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert "large_trades" in signal.reasons


def test_large_trades_sell(whale_sell_snapshot):
    """Много крупных сделок + tfi_1m отрицательный → SELL."""
    signal = rule_large_trades(whale_sell_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.SELL
    assert "large_trades" in signal.reasons


def test_large_trades_none_few_whales(few_large_trades_snapshot):
    """Крупных сделок мало (ниже порога) → None даже при сильном TFI."""
    signal = rule_large_trades(few_large_trades_snapshot)

    assert signal is None


# ============================================================
# Правило 4: rule_exhaustion
# ============================================================

def test_exhaustion_buy(exhaustion_buy_snapshot):
    """tfi_5m << 0 (медведи устали) + tfi_30s > 0 (разворот) → BUY."""
    signal = rule_exhaustion(exhaustion_buy_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert "exhaustion" in signal.reasons


def test_exhaustion_sell(exhaustion_sell_snapshot):
    """tfi_5m >> 0 (быки устали) + tfi_30s < 0 (разворот) → SELL."""
    signal = rule_exhaustion(exhaustion_sell_snapshot)

    assert signal is not None
    assert signal.action == SignalAction.SELL
    assert "exhaustion" in signal.reasons


def test_exhaustion_none_no_reversal(exhaustion_no_reversal_snapshot):
    """Истощение есть, но разворота на 30s нет → None."""
    signal = rule_exhaustion(exhaustion_no_reversal_snapshot)

    assert signal is None