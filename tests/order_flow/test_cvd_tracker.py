"""
Unit-тесты для app/engines/order_flow/cvd_tracker.py

8 тестов покрывают:
- get для неизвестного символа -> 0.0
- update прибавляет дельту
- update накапливает (несколько вызовов)
- update с отрицательной дельтой (sell-pressure)
- update возвращает новое значение
- reset одного символа
- reset всех символов (без аргумента)
- get_all возвращает копию (мутация снаружи не влияет)
"""

from __future__ import annotations

import math

import pytest

from app.engines.order_flow.cvd_tracker import CVDTracker


# Все тесты async - помечаем модуль целиком.
pytestmark = pytest.mark.asyncio


class TestCVDTracker:
    """Тесты для CVDTracker."""

    async def test_get_unknown_symbol_returns_zero(self):
        """Новый символ - cvd = 0.0."""
        tracker = CVDTracker()
        assert await tracker.get("BTCUSDT") == 0.0

    async def test_update_adds_delta(self):
        """После update(1.5) значение = 1.5."""
        tracker = CVDTracker()
        await tracker.update("BTCUSDT", 1.5)
        assert math.isclose(await tracker.get("BTCUSDT"), 1.5, rel_tol=1e-9)

    async def test_update_accumulates(self):
        """Несколько update подряд накапливаются."""
        tracker = CVDTracker()
        await tracker.update("BTCUSDT", 1.0)
        await tracker.update("BTCUSDT", 2.5)
        await tracker.update("BTCUSDT", 0.5)
        assert math.isclose(await tracker.get("BTCUSDT"), 4.0, rel_tol=1e-9)

    async def test_update_negative_delta(self):
        """Отрицательная дельта (sell-pressure) уменьшает CVD."""
        tracker = CVDTracker()
        await tracker.update("BTCUSDT", 5.0)
        await tracker.update("BTCUSDT", -2.0)
        assert math.isclose(await tracker.get("BTCUSDT"), 3.0, rel_tol=1e-9)

    async def test_update_returns_new_value(self):
        """update возвращает новое значение CVD сразу."""
        tracker = CVDTracker()
        result = await tracker.update("BTCUSDT", 1.7)
        assert math.isclose(result, 1.7, rel_tol=1e-9)
        result2 = await tracker.update("BTCUSDT", 0.3)
        assert math.isclose(result2, 2.0, rel_tol=1e-9)

    async def test_reset_single_symbol(self):
        """reset("BTCUSDT") очищает только BTC, остальные не трогает."""
        tracker = CVDTracker()
        await tracker.update("BTCUSDT", 10.0)
        await tracker.update("ETHUSDT", 5.0)
        await tracker.reset("BTCUSDT")
        assert await tracker.get("BTCUSDT") == 0.0
        assert math.isclose(await tracker.get("ETHUSDT"), 5.0, rel_tol=1e-9)

    async def test_reset_all_no_args(self):
        """reset() без аргумента очищает все символы."""
        tracker = CVDTracker()
        await tracker.update("BTCUSDT", 10.0)
        await tracker.update("ETHUSDT", 5.0)
        await tracker.reset()
        assert await tracker.get("BTCUSDT") == 0.0
        assert await tracker.get("ETHUSDT") == 0.0
        assert await tracker.get_all() == {}

    async def test_get_all_returns_copy(self):
        """get_all возвращает копию - внешняя мутация не влияет на tracker."""
        tracker = CVDTracker()
        await tracker.update("BTCUSDT", 1.0)
        snapshot = await tracker.get_all()
        snapshot["BTCUSDT"] = 999.0
        snapshot["HACKERUSDT"] = -1.0
        # Внутреннее состояние не изменилось
        assert math.isclose(await tracker.get("BTCUSDT"), 1.0, rel_tol=1e-9)
        assert await tracker.get("HACKERUSDT") == 0.0