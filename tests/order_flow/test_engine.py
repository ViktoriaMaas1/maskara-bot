"""
Unit-тесты для app/engines/order_flow/engine.py

12 тестов покрывают:
- get_snapshot со здоровыми данными (метрики правильно посчитаны)
- get_snapshot с пустым кешем (data_available=False, всё нули)
- get_snapshot без orderbook (только trades) -> obi=0, остальные ок
- get_snapshot без trades (только orderbook) -> только obi заполнен
- get_snapshot: orderbook_age_ms правильно рассчитан
- get_snapshot: symbol нормализуется (lowercase -> uppercase)
- update_cvd прибавляет дельту, возвращает новое значение
- update_cvd с пустыми trades не падает
- get_cvd / reset_cvd / get_all_cvd работают
- Singleton: init + get
- Singleton: get до init -> исключение
- Singleton: close + повторный get -> исключение
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest

from app.engines.order_flow.engine import (
    OrderFlowEngine,
    OrderFlowEngineNotInitialized,
    close_order_flow_engine,
    get_order_flow_engine,
    init_order_flow_engine,
)


pytestmark = pytest.mark.asyncio


# ============================================================
# Хелпер - фейковый MarketCache
# ============================================================

def make_fake_cache(trades=None, orderbook=None):
    """Создаёт AsyncMock с заданными возвращаемыми значениями."""
    cache = AsyncMock()
    cache.get_trades = AsyncMock(return_value=trades or [])
    cache.get_orderbook = AsyncMock(return_value=orderbook)
    return cache


# ============================================================
# Фикстуры специфичные для engine
# ============================================================

@pytest.fixture
def engine_trades():
    """Свежие trades (за последние секунды). Используем datetime.now для актуальности."""
    import time as _time
    now = int(_time.time() * 1000)
    return [
        {"ts": now - 5_000, "side": "Buy", "price": "80000", "qty": "1.0", "tradeId": "e1"},
        {"ts": now - 10_000, "side": "Sell", "price": "80000", "qty": "0.5", "tradeId": "e2"},
        {"ts": now - 15_000, "side": "Buy", "price": "80000", "qty": "2.0", "tradeId": "e3"},
    ]


@pytest.fixture
def engine_orderbook():
    """Свежий orderbook. ts близок к now."""
    import time as _time
    now = int(_time.time() * 1000)
    return {
        "b": [["80000", "1.0"], ["79999", "1.0"], ["79998", "1.0"],
              ["79997", "1.0"], ["79996", "1.0"]],
        "a": [["80001", "0.5"], ["80002", "0.5"], ["80003", "0.5"],
              ["80004", "0.5"], ["80005", "0.5"]],
        "ts": now - 100,
        "u": 1,
        "seq": 1,
    }


# ============================================================
# get_snapshot
# ============================================================

class TestGetSnapshot:
    """Тесты для OrderFlowEngine.get_snapshot()."""

    async def test_healthy_data_metrics_correct(self, engine_trades, engine_orderbook):
        """Snapshot со свежими trades+orderbook: метрики ненулевые, data_available=True."""
        cache = make_fake_cache(trades=engine_trades, orderbook=engine_orderbook)
        engine = OrderFlowEngine(cache)
        snap = await engine.get_snapshot("BTCUSDT")

        assert snap.symbol == "BTCUSDT"
        assert snap.data_available is True
        # delta: 1.0+2.0 - 0.5 = 2.5
        assert math.isclose(snap.delta_1m, 2.5, rel_tol=1e-6)
        # tfi: 2.5/3.5
        assert math.isclose(snap.tfi_1m, 2.5 / 3.5, rel_tol=1e-6)
        # obi top-5: bid 5.0, ask 2.5 -> (5-2.5)/7.5 = 1/3
        assert math.isclose(snap.obi_top5, 1.0 / 3.0, rel_tol=1e-6)
        # CVD пока 0 (мы не вызывали update_cvd)
        assert snap.cvd == 0.0
        assert snap.total_volume_1m > 0

    async def test_empty_cache_data_unavailable(self):
        """Snapshot при пустом кеше: data_available=False, всё нули."""
        cache = make_fake_cache(trades=[], orderbook=None)
        engine = OrderFlowEngine(cache)
        snap = await engine.get_snapshot("BTCUSDT")

        assert snap.data_available is False
        assert snap.delta_1m == 0.0
        assert snap.tfi_1m == 0.0
        assert snap.obi_top5 == 0.0
        assert snap.cvd == 0.0
        assert snap.large_trade_count_1m == 0
        assert snap.orderbook_age_ms is None

    async def test_only_trades_no_orderbook(self, engine_trades):
        """Есть trades, нет orderbook: delta/tfi работают, obi=0."""
        cache = make_fake_cache(trades=engine_trades, orderbook=None)
        engine = OrderFlowEngine(cache)
        snap = await engine.get_snapshot("BTCUSDT")

        assert snap.data_available is True
        assert snap.delta_1m > 0
        assert snap.obi_top5 == 0.0
        assert snap.orderbook_age_ms is None

    async def test_only_orderbook_no_trades(self, engine_orderbook):
        """Есть orderbook, нет trades: obi работает, delta=0."""
        cache = make_fake_cache(trades=[], orderbook=engine_orderbook)
        engine = OrderFlowEngine(cache)
        snap = await engine.get_snapshot("BTCUSDT")

        assert snap.data_available is True
        assert snap.delta_1m == 0.0
        assert snap.obi_top5 > 0
        assert snap.orderbook_age_ms is not None
        assert snap.orderbook_age_ms >= 0

    async def test_orderbook_age_calculated(self, engine_orderbook):
        """orderbook_age_ms = now - ts, в разумных пределах."""
        cache = make_fake_cache(trades=[], orderbook=engine_orderbook)
        engine = OrderFlowEngine(cache)
        snap = await engine.get_snapshot("BTCUSDT")

        # orderbook был создан за ~100мс до snapshot
        assert snap.orderbook_age_ms is not None
        assert 0 <= snap.orderbook_age_ms < 60_000  # меньше минуты

    async def test_symbol_normalized_to_upper(self, engine_trades):
        """Lowercase symbol -> в snapshot всё равно uppercase."""
        cache = make_fake_cache(trades=engine_trades)
        engine = OrderFlowEngine(cache)
        snap = await engine.get_snapshot("btcusdt")
        assert snap.symbol == "BTCUSDT"


# ============================================================
# update_cvd
# ============================================================

class TestUpdateCVD:
    """Тесты для OrderFlowEngine.update_cvd()."""

    async def test_update_cvd_adds_delta(self, engine_trades):
        """update_cvd прибавляет дельту за окно к CVD."""
        cache = make_fake_cache(trades=engine_trades)
        engine = OrderFlowEngine(cache)

        new_cvd = await engine.update_cvd("BTCUSDT")
        # delta = 1.0 + 2.0 - 0.5 = 2.5
        assert math.isclose(new_cvd, 2.5, rel_tol=1e-6)
        # повторно - снова прибавится (CVD = 5.0)
        new_cvd2 = await engine.update_cvd("BTCUSDT")
        assert math.isclose(new_cvd2, 5.0, rel_tol=1e-6)

    async def test_update_cvd_no_trades_returns_current(self):
        """Если trades пустые - CVD не меняется, возвращается текущее."""
        cache = make_fake_cache(trades=[])
        engine = OrderFlowEngine(cache)

        result = await engine.update_cvd("BTCUSDT")
        assert result == 0.0


# ============================================================
# CVD helpers
# ============================================================

class TestCVDHelpers:
    """Тесты для get_cvd / reset_cvd / get_all_cvd."""

    async def test_cvd_helpers_work(self, engine_trades):
        """get_cvd, reset_cvd, get_all_cvd корректно делегируют CVDTracker."""
        cache = make_fake_cache(trades=engine_trades)
        engine = OrderFlowEngine(cache)

        # Сначала CVD = 0
        assert await engine.get_cvd("BTCUSDT") == 0.0

        # После update_cvd значение есть
        await engine.update_cvd("BTCUSDT")
        assert await engine.get_cvd("BTCUSDT") > 0

        # get_all возвращает дикт
        all_cvd = await engine.get_all_cvd()
        assert "BTCUSDT" in all_cvd

        # reset обнуляет
        await engine.reset_cvd("BTCUSDT")
        assert await engine.get_cvd("BTCUSDT") == 0.0


# ============================================================
# Singleton lifecycle
# ============================================================

class TestSingleton:
    """Тесты для init_order_flow_engine / get_order_flow_engine / close_order_flow_engine."""

    async def test_init_and_get(self):
        """После init_order_flow_engine() singleton доступен через get."""
        close_order_flow_engine()  # на всякий случай сбросим
        cache = make_fake_cache()
        instance = init_order_flow_engine(cache)
        retrieved = get_order_flow_engine()
        assert retrieved is instance
        close_order_flow_engine()  # cleanup

    async def test_get_before_init_raises(self):
        """get_order_flow_engine() до init -> исключение."""
        close_order_flow_engine()  # гарантируем что не инициализирован
        with pytest.raises(OrderFlowEngineNotInitialized):
            get_order_flow_engine()

    async def test_close_then_get_raises(self):
        """После close - get снова кидает исключение."""
        cache = make_fake_cache()
        init_order_flow_engine(cache)
        close_order_flow_engine()
        with pytest.raises(OrderFlowEngineNotInitialized):
            get_order_flow_engine()