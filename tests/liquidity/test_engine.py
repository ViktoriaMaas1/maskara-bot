"""
Тесты для Liquidity Engine.

Mock'им MarketCache и проверяем, что engine правильно читает и обрабатывает данные.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.engines.liquidity.engine import LiquidityEngine
from app.engines.liquidity.models import ZoneSide


@pytest.fixture
def mock_cache(sample_orderbook_with_walls, sample_trades_with_extremes, sample_liquidations_valid):
    """Mock MarketCache с реальными данными."""
    cache = MagicMock()
    cache.get_orderbook = AsyncMock(return_value=sample_orderbook_with_walls)
    cache.get_trades = AsyncMock(return_value=sample_trades_with_extremes)
    cache.get_liquidations = AsyncMock(return_value=sample_liquidations_valid)
    return cache


@pytest.fixture
def engine(mock_cache):
    """LiquidityEngine с mock'ом cache."""
    return LiquidityEngine(mock_cache)


class TestLiquidityEngine:
    """Тесты engine'а."""

    @pytest.mark.asyncio
    async def test_get_snapshot_success(self, engine, mock_cache):
        """engine должен собрать полный snapshot."""
        snapshot = await engine.get_snapshot("BTCUSDT")

        assert snapshot.symbol == "BTCUSDT"
        assert snapshot.data_available is True
        assert snapshot.mid_price > 0
        assert len(snapshot.zones_below) > 0 or len(snapshot.zones_above) > 0
        assert snapshot.local_high is not None
        assert snapshot.local_low is not None
        assert snapshot.recent_liquidation_count == 3

    @pytest.mark.asyncio
    async def test_get_snapshot_empty_orderbook(self, engine):
        """Если orderbook пуст — snapshot.data_available=False."""
        engine._cache.get_orderbook = AsyncMock(return_value={"b": [], "a": []})
        snapshot = await engine.get_snapshot("BTCUSDT")

        assert snapshot.data_available is False
        assert snapshot.zones_below == []
        assert snapshot.zones_above == []

    @pytest.mark.asyncio
    async def test_get_snapshot_exception_handling(self, engine):
        """При исключении engine возвращает пустой snapshot (не падает)."""
        engine._cache.get_orderbook = AsyncMock(side_effect=Exception("Cache error"))
        snapshot = await engine.get_snapshot("BTCUSDT")

        assert snapshot.data_available is False
        assert snapshot.symbol == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_zones_above_have_correct_prices(self, engine):
        """Зоны выше должны быть выше mid_price."""
        snapshot = await engine.get_snapshot("BTCUSDT")

        for zone in snapshot.zones_above:
            assert zone.side == ZoneSide.ABOVE
            assert zone.price > snapshot.mid_price

    @pytest.mark.asyncio
    async def test_zones_below_have_correct_prices(self, engine):
        """Зоны ниже должны быть ниже mid_price."""
        snapshot = await engine.get_snapshot("BTCUSDT")

        for zone in snapshot.zones_below:
            assert zone.side == ZoneSide.BELOW
            assert zone.price < snapshot.mid_price
