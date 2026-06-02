"""
Liquidity Engine — анализ ликвидности из market_cache.

Читает orderbook, trades, liquidations и собирает LiquiditySnapshot.
Паттерн: как OrderFlowEngine, но для зон ликвидности.
"""

import logging
import time
from typing import Optional

from app.cache.market_cache import MarketCache

from .detector import (
    count_recent_liquidations,
    detect_local_highs_lows,
    find_orderbook_walls,
)
from .models import LiquiditySnapshot

logger = logging.getLogger(__name__)

# ====================================================================
# Exceptions
# ====================================================================


class LiquidityEngineError(Exception):
    """Базовая ошибка Liquidity Engine."""

    pass


class LiquidityEngineNotInitialized(LiquidityEngineError):
    """Singleton не инициализирован через init_liquidity_engine()."""

    pass


# ====================================================================
# Engine
# ====================================================================

# Singleton для инъекции в API
_liquidity_engine: Optional["LiquidityEngine"] = None


def init_liquidity_engine(cache: MarketCache) -> None:
    """Инициализировать singleton Liquidity Engine."""
    global _liquidity_engine
    _liquidity_engine = LiquidityEngine(cache)
    logger.info("✓ Liquidity Engine initialized")


def get_liquidity_engine() -> "LiquidityEngine":
    """Получить singleton (raises если не инициализирован)."""
    if _liquidity_engine is None:
        raise LiquidityEngineNotInitialized("Call init_liquidity_engine() first")
    return _liquidity_engine


class LiquidityEngine:
    """Главный класс анализа ликвидности."""

    def __init__(self, cache: MarketCache) -> None:
        """
        Конструктор.

        Args:
            cache: MarketCache для чтения orderbook/trades/liquidations
        """
        self._cache = cache
        logger.info("LiquidityEngine created")

    async def get_snapshot(self, symbol: str) -> LiquiditySnapshot:
        """
        Собрать снимок ликвидности для символа.

        Читает из cache и возвращает LiquiditySnapshot.
        Если данных нет — возвращает пустой snapshot с data_available=False.

        Args:
            symbol: торговый символ (например BTCUSDT)

        Returns:
            LiquiditySnapshot (всегда HTTP 200, фронт сам решает что показывать)
        """
        try:
            timestamp_ms = int(time.time() * 1000)

            # Читаем данные из cache
            orderbook = await self._cache.get_orderbook(symbol)
            trades = await self._cache.get_trades(symbol, limit=50)
            liquidations = await self._cache.get_liquidations(symbol, limit=50)

            # Если нет orderbook — возвращаем пустой snapshot
            if not orderbook or not orderbook.get("b") and not orderbook.get("a"):
                logger.debug(f"No orderbook data for {symbol}")
                return LiquiditySnapshot(
                    symbol=symbol,
                    timestamp_ms=timestamp_ms,
                    data_available=False,
                )

            # Вычисляем mid_price
            bids = orderbook.get("b", [])
            asks = orderbook.get("a", [])

            if not bids or not asks:
                return LiquiditySnapshot(
                    symbol=symbol,
                    timestamp_ms=timestamp_ms,
                    data_available=False,
                )

            bid_price = float(bids[0][0]) if bids else 0
            ask_price = float(asks[0][0]) if asks else 0

            if bid_price <= 0 or ask_price <= 0:
                return LiquiditySnapshot(
                    symbol=symbol,
                    timestamp_ms=timestamp_ms,
                    data_available=False,
                )

            mid_price = (bid_price + ask_price) / 2

            # Детекторы — чистые функции, работают параллельно
            zones_below, zones_above = find_orderbook_walls(orderbook, mid_price)
            local_high, local_low = detect_local_highs_lows(trades, window_size=50)
            liq_count = count_recent_liquidations(liquidations)

            # Собираем snapshot
            snapshot = LiquiditySnapshot(
                symbol=symbol,
                timestamp_ms=timestamp_ms,
                data_available=True,
                mid_price=mid_price,
                zones_above=zones_above,
                zones_below=zones_below,
                local_high=local_high,
                local_low=local_low,
                recent_liquidation_count=liq_count,
            )

            logger.debug(
                f"Liquidity snapshot {symbol}: "
                f"mid={mid_price:.2f}, "
                f"zones={len(zones_below)}↓/{len(zones_above)}↑, "
                f"liq={liq_count}"
            )

            return snapshot

        except Exception as e:
            logger.exception(f"Error in get_snapshot({symbol}): {e}")
            # При ошибке тоже возвращаем snapshot, но с data_available=False
            return LiquiditySnapshot(
                symbol=symbol,
                timestamp_ms=int(time.time() * 1000),
                data_available=False,
            )
