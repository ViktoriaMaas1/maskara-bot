"""
Unit-тесты для app/engines/order_flow/metrics.py

15 тестов покрывают:
- compute_delta (3 теста)
- compute_tfi (3 теста)
- compute_obi (4 теста)
- compute_aggression (2 теста)
- detect_large_trades (3 теста)

Все тесты детерминированные: time приходит как фикстура now_ms.
"""

from __future__ import annotations

import math

from app.engines.order_flow.metrics import (
    compute_aggression,
    compute_delta,
    compute_obi,
    compute_tfi,
    detect_large_trades,
)


# ============================================================
# compute_delta
# ============================================================

class TestComputeDelta:
    """Тесты для compute_delta()."""

    def test_balanced_returns_zero(self, balanced_trades, now_ms):
        """Buy == Sell -> delta = 0."""
        result = compute_delta(balanced_trades, window_seconds=60, now_ms=now_ms)
        assert result == 0.0

    def test_buy_dominance_returns_positive(self, simple_trades, now_ms):
        """3.0 Buy - 0.5 Sell = 2.5."""
        result = compute_delta(simple_trades, window_seconds=60, now_ms=now_ms)
        assert math.isclose(result, 2.5, rel_tol=1e-9)

    def test_old_trades_outside_window_excluded(self, trades_with_old, now_ms):
        """В окне 60с только 1 Buy 1.0 -> delta = 1.0 (старые отброшены)."""
        result = compute_delta(trades_with_old, window_seconds=60, now_ms=now_ms)
        assert math.isclose(result, 1.0, rel_tol=1e-9)


# ============================================================
# compute_tfi
# ============================================================

class TestComputeTFI:
    """Тесты для compute_tfi()."""

    def test_balanced_returns_zero(self, balanced_trades, now_ms):
        """Buy == Sell -> tfi = 0."""
        result = compute_tfi(balanced_trades, window_seconds=60, now_ms=now_ms)
        assert result == 0.0

    def test_all_buys_returns_one(self, only_buy_trades, now_ms):
        """Все Buy -> tfi = +1.0 (максимум)."""
        result = compute_tfi(only_buy_trades, window_seconds=60, now_ms=now_ms)
        assert math.isclose(result, 1.0, rel_tol=1e-9)

    def test_empty_returns_zero(self, now_ms):
        """Пустой список -> tfi = 0 (нейтрально)."""
        result = compute_tfi([], window_seconds=60, now_ms=now_ms)
        assert result == 0.0


# ============================================================
# compute_obi
# ============================================================

class TestComputeOBI:
    """Тесты для compute_obi()."""

    def test_bids_thicker_returns_positive(self, simple_orderbook):
        """Bids толще asks на top-5 -> obi > 0."""
        result = compute_obi(simple_orderbook, depth=5)
        # bid=5.0, ask=2.5, total=7.5, obi=2.5/7.5≈0.333
        assert math.isclose(result, 1.0 / 3.0, rel_tol=1e-6)

    def test_depth_changes_result(self, deep_orderbook):
        """Глубина 5 (bids толще) vs глубина 20 (asks толще) - разные знаки."""
        obi_5 = compute_obi(deep_orderbook, depth=5)
        obi_20 = compute_obi(deep_orderbook, depth=20)
        assert obi_5 > 0
        assert obi_20 < 0

    def test_none_returns_zero(self):
        """orderbook=None -> obi = 0 (нейтрально)."""
        assert compute_obi(None, depth=5) == 0.0

    def test_garbage_levels_skipped(self, garbage_orderbook):
        """Битые уровни не должны валить функцию.

        Валидные bids: 1.0 + 0.5 = 1.5
        Валидные asks: 0.5 + 0.5 = 1.0
        obi = (1.5 - 1.0) / 2.5 = 0.2
        """
        result = compute_obi(garbage_orderbook, depth=10)
        assert math.isclose(result, 0.2, rel_tol=1e-6)


# ============================================================
# compute_aggression
# ============================================================

class TestComputeAggression:
    """Тесты для compute_aggression()."""

    def test_buy_aggression_correct(self, simple_trades, now_ms):
        """3.0 Buy / 3.5 total ≈ 0.857."""
        result = compute_aggression(simple_trades, window_seconds=60, now_ms=now_ms)
        assert math.isclose(result["buy_aggression"], 3.0 / 3.5, rel_tol=1e-6)
        assert math.isclose(result["total_volume"], 3.5, rel_tol=1e-9)
        assert result["trades_count"] == 3

    def test_empty_returns_zero_count(self, now_ms):
        """Пустой список -> trades_count = 0, всё нули."""
        result = compute_aggression([], window_seconds=60, now_ms=now_ms)
        assert result["buy_aggression"] == 0.0
        assert result["total_volume"] == 0.0
        assert result["trades_count"] == 0


# ============================================================
# detect_large_trades
# ============================================================

class TestDetectLargeTrades:
    """Тесты для detect_large_trades()."""

    def test_few_trades_returns_zero(self, simple_trades, now_ms):
        """Меньше 10 trade -> 0 (защита от шума)."""
        result = detect_large_trades(simple_trades, window_seconds=60, now_ms=now_ms)
        assert result == 0

    def test_whale_detected(self, many_trades_with_whale, now_ms):
        """15 trade: 14 мелких (0.1) + 1 кит (10.0). Percentile=95 -> 1 кит."""
        result = detect_large_trades(
            many_trades_with_whale, window_seconds=60, now_ms=now_ms,
        )
        assert result == 1

    def test_uniform_trades_no_whale(self, many_uniform_trades, now_ms):
        """15 одинаковых trade -> кит не найден (порог = сам qty)."""
        result = detect_large_trades(
            many_uniform_trades, window_seconds=60, now_ms=now_ms,
        )
        assert result == 0