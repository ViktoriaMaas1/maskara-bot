"""
OrderFlowEngine - главный класс Stage 7.

Связывает MarketCache (источник данных) и CVDTracker (in-memory state).
Использует чистые функции metrics.py для расчётов.

Ключевые принципы:
- get_snapshot() - read-only, НЕ обновляет CVD. Можно вызывать сколько угодно
  раз без побочных эффектов (важно для API endpoint).
- update_cvd() - вызывается отдельно (периодически из Stage 8). Берёт дельту
  за фиксированное окно и накапливает в CVDTracker.
- data_available=False, если в кеше нет ни trades, ни orderbook (WebSocket лёг).
  Сигнал для Signal Engine: "не торгуй".

Singleton-паттерн: init_order_flow_engine() / get_order_flow_engine() /
close_order_flow_engine(). Аналогично get_market_cache() из Stage 6.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.cache.market_cache import MarketCache
from app.engines.order_flow.cvd_tracker import CVDTracker
from app.engines.order_flow.metrics import (
    compute_aggression,
    compute_delta,
    compute_obi,
    compute_tfi,
    detect_large_trades,
)
from app.engines.order_flow.models import (
    LARGE_TRADE_PERCENTILE,
    LARGE_TRADE_WINDOW_SEC,
    OBI_DEPTHS,
    OrderFlowSnapshot,
    WINDOWS_SECONDS,
)

logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class OrderFlowEngineError(Exception):
    """Базовая ошибка Order Flow Engine."""
    pass


class OrderFlowEngineNotInitialized(OrderFlowEngineError):
    """Singleton не инициализирован через init_order_flow_engine()."""
    pass


# ============================================================
# Engine
# ============================================================

# Какое окно используется для update_cvd (1 минута).
# Не путать с WINDOWS_SECONDS (это окна snapshot-метрик).
CVD_UPDATE_WINDOW_SEC = 60

# Сколько trades забирать из кеша при расчёте snapshot.
# 500 - максимум, что хранит MarketCache.
TRADES_FETCH_LIMIT = 500


class OrderFlowEngine:
    """Главный класс Order Flow Engine."""

    def __init__(self, cache: MarketCache) -> None:
        self._cache = cache
        self._cvd = CVDTracker()

    async def get_snapshot(self, symbol: str) -> OrderFlowSnapshot:
        """Собрать полный snapshot метрик для символа.

        READ-ONLY: не меняет состояние CVDTracker.
        Возвращает Snapshot с data_available=False, если кеш пустой.
        """
        symbol = symbol.upper()
        now_ms = int(time.time() * 1000)

        trades = await self._cache.get_trades(symbol, limit=TRADES_FETCH_LIMIT)
        orderbook = await self._cache.get_orderbook(symbol)

        # Если нет ни trades, ни orderbook - WebSocket лёг или символ новый.
        data_available = bool(trades) or bool(orderbook)

        # Дельта по окнам.
        delta_30s = compute_delta(trades, 30, now_ms) if trades else 0.0
        delta_1m = compute_delta(trades, 60, now_ms) if trades else 0.0
        delta_5m = compute_delta(trades, 300, now_ms) if trades else 0.0

        # TFI по окнам.
        tfi_30s = compute_tfi(trades, 30, now_ms) if trades else 0.0
        tfi_1m = compute_tfi(trades, 60, now_ms) if trades else 0.0
        tfi_5m = compute_tfi(trades, 300, now_ms) if trades else 0.0

        # Агрессия за 1м.
        aggr = (
            compute_aggression(trades, 60, now_ms)
            if trades
            else {"buy_aggression": 0.0, "total_volume": 0.0, "trades_count": 0}
        )

        # OBI на разных глубинах.
        obi_top5 = compute_obi(orderbook, 5)
        obi_top10 = compute_obi(orderbook, 10)
        obi_top20 = compute_obi(orderbook, 20)

        # Крупные сделки за 1м.
        large_count = (
            detect_large_trades(
                trades, LARGE_TRADE_WINDOW_SEC, now_ms, LARGE_TRADE_PERCENTILE,
            )
            if trades
            else 0
        )

        # CVD (только чтение!).
        cvd_value = await self._cvd.get(symbol)

        # Возраст orderbook (для отладки и Signal Engine).
        orderbook_age_ms: Optional[int] = None
        if orderbook and "ts" in orderbook:
            try:
                ob_ts = int(orderbook["ts"])
                orderbook_age_ms = max(0, now_ms - ob_ts)
            except (ValueError, TypeError):
                orderbook_age_ms = None

        return OrderFlowSnapshot(
            symbol=symbol,
            timestamp_ms=now_ms,
            data_available=data_available,
            delta_30s=delta_30s,
            delta_1m=delta_1m,
            delta_5m=delta_5m,
            cvd=cvd_value,
            obi_top5=obi_top5,
            obi_top10=obi_top10,
            obi_top20=obi_top20,
            tfi_30s=tfi_30s,
            tfi_1m=tfi_1m,
            tfi_5m=tfi_5m,
            buy_aggression_1m=aggr["buy_aggression"],
            total_volume_1m=aggr["total_volume"],
            large_trade_count_1m=large_count,
            trades_count_1m=aggr["trades_count"],
            orderbook_age_ms=orderbook_age_ms,
        )

    async def update_cvd(self, symbol: str) -> float:
        """Прибавить дельту за окно CVD_UPDATE_WINDOW_SEC к CVD символа.

        Вызывается отдельно (periodic-job в Stage 8). Несколько вызовов
        подряд приведут к накрутке CVD - вызывающий должен соблюдать интервал.
        """
        symbol = symbol.upper()
        now_ms = int(time.time() * 1000)

        trades = await self._cache.get_trades(symbol, limit=TRADES_FETCH_LIMIT)
        if not trades:
            # Нет данных - возвращаем текущее значение без изменений.
            return await self._cvd.get(symbol)

        delta = compute_delta(trades, CVD_UPDATE_WINDOW_SEC, now_ms)
        new_cvd = await self._cvd.update(symbol, delta)
        logger.debug(
            "CVD updated: symbol=%s delta=%.4f new_cvd=%.4f",
            symbol, delta, new_cvd,
        )
        return new_cvd

    async def get_cvd(self, symbol: str) -> float:
        """Текущий CVD символа."""
        return await self._cvd.get(symbol.upper())

    async def reset_cvd(self, symbol: Optional[str] = None) -> None:
        """Сбросить CVD: символа или все."""
        if symbol is not None:
            symbol = symbol.upper()
        await self._cvd.reset(symbol)

    async def get_all_cvd(self) -> dict[str, float]:
        """Все CVD сразу (для дашборда)."""
        return await self._cvd.get_all()


# ============================================================
# Singleton lifecycle
# ============================================================

_engine: Optional[OrderFlowEngine] = None


def init_order_flow_engine(cache: MarketCache) -> OrderFlowEngine:
    """Создать singleton. Вызывается из FastAPI lifespan() при старте."""
    global _engine
    if _engine is not None:
        logger.warning("OrderFlowEngine: уже инициализирован, переинициализация")
    _engine = OrderFlowEngine(cache)
    logger.info("OrderFlowEngine initialized")
    return _engine


def get_order_flow_engine() -> OrderFlowEngine:
    """Получить singleton. Должен быть уже инициализирован."""
    if _engine is None:
        raise OrderFlowEngineNotInitialized(
            "OrderFlowEngine не инициализирован. Это значит lifespan() не отработал - проверь main.py"
        )
    return _engine


def close_order_flow_engine() -> None:
    """Закрыть singleton. Вызывается из FastAPI lifespan() при остановке."""
    global _engine
    if _engine is not None:
        _engine = None
        logger.info("OrderFlowEngine closed")