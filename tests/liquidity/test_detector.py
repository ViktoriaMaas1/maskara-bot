"""
Тесты для Liquidity Detector.

Проверяют три функции: find_orderbook_walls, detect_local_highs_lows, count_recent_liquidations
на реальных форматах данных.
"""

import pytest

from app.engines.liquidity.detector import (
    find_orderbook_walls,
    detect_local_highs_lows,
    count_recent_liquidations,
)
from app.engines.liquidity.models import ZoneSide


class TestFindOrderbookWalls:
    """Тесты для detect_orderbook_walls."""

    def test_walls_detected_in_orderbook(self, sample_orderbook_with_walls):
        """Стенки в стакане должны быть найдены и отсортированы по близости."""
        mid_price = 69250.0  # Между bids (69000) и asks (69500)
        zones_below, zones_above = find_orderbook_walls(sample_orderbook_with_walls, mid_price)

        # Ожидаем найти стенки
        assert len(zones_below) > 0, "Должны найти зоны ниже цены"
        assert len(zones_above) > 0, "Должны найти зоны выше цены"

        # Проверяем, что стенки отсортированы по близости (первые — ближайшие)
        if len(zones_below) > 1:
            assert zones_below[0].distance_pct >= zones_below[-1].distance_pct

        if len(zones_above) > 1:
            assert zones_above[0].distance_pct <= zones_above[-1].distance_pct

    def test_empty_orderbook(self, sample_orderbook_empty):
        """Пустой orderbook должен вернуть пустые списки."""
        mid_price = 69250.0
        zones_below, zones_above = find_orderbook_walls(sample_orderbook_empty, mid_price)

        assert zones_below == []
        assert zones_above == []

    def test_zones_have_correct_side(self, sample_orderbook_with_walls):
        """Зоны должны иметь правильные ABOVE/BELOW стороны."""
        mid_price = 69250.0
        zones_below, zones_above = find_orderbook_walls(sample_orderbook_with_walls, mid_price)

        for zone in zones_below:
            assert zone.side == ZoneSide.BELOW
            assert zone.price < mid_price

        for zone in zones_above:
            assert zone.side == ZoneSide.ABOVE
            assert zone.price > mid_price


class TestDetectLocalHighsLows:
    """Тесты для detect_local_highs_lows."""

    def test_finds_extremes_in_trades(self, sample_trades_with_extremes):
        """Должны найти правильные локальные макс и мин."""
        high, low = detect_local_highs_lows(sample_trades_with_extremes)

        assert high == 69500.0, "Max должен быть 69500"
        assert low == 69100.0, "Min должен быть 69100"

    def test_empty_trades_list(self):
        """Пустой список сделок должен вернуть (None, None)."""
        high, low = detect_local_highs_lows([])

        assert high is None
        assert low is None

    def test_window_size_limit(self, sample_trades_with_extremes):
        """Window size должен ограничить количество сделок."""
        high, low = detect_local_highs_lows(sample_trades_with_extremes, window_size=2)

        # С окном в 2 сделки мы пропустим самый нижний элемент
        assert high == 69400.0  # Первые 2 сделки
        assert low == 69200.0


class TestCountRecentLiquidations:
    """Тесты для count_recent_liquidations."""

    def test_counts_valid_liquidations(self, sample_liquidations_valid):
        """Должны считать только валидные ликвидации."""
        count = count_recent_liquidations(sample_liquidations_valid)

        assert count == 3, "Все 3 ликвидации валидны"

    def test_filters_junk_liquidations(self, sample_liquidations_with_junk):
        """Должны отфильтровать ликвидации с нулевой ценой и пустым side."""
        count = count_recent_liquidations(sample_liquidations_with_junk)

        assert count == 2, "Только 2 из 4 ликвидаций валидны"

    def test_empty_liquidations(self, sample_liquidations_empty):
        """Пустой список должен вернуть 0."""
        count = count_recent_liquidations(sample_liquidations_empty)

        assert count == 0
