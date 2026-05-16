"""
CVDTracker - in-memory счётчик Cumulative Volume Delta для каждого символа.

CVD = накопленная сумма всех дельт (buy_volume - sell_volume).
- CVD растёт -> устойчивое давление покупателей
- CVD падает -> устойчивое давление продавцов
- Дивергенция CVD vs price - мощный сигнал разворота (Stage 8+)

Все методы async + защищены asyncio.Lock от race conditions при
конкурентных вызовах из разных задач (WebSocket collector, API endpoint,
periodic snapshot job).

In-memory: при рестарте контейнера CVD сбрасывается. В Stage 8+ перенесём
в Redis для перезапусков без потери накопленной истории.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class CVDTracker:
    """Накопленная дельта на символ. Thread-safe для asyncio."""

    def __init__(self) -> None:
        self._values: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def update(self, symbol: str, delta: float) -> float:
        """Прибавить delta к CVD символа. Возвращает новое значение."""
        async with self._lock:
            current = self._values.get(symbol, 0.0)
            new_value = current + delta
            self._values[symbol] = new_value
            return new_value

    async def get(self, symbol: str) -> float:
        """Текущий CVD символа. 0.0 если символ ещё не встречался."""
        async with self._lock:
            return self._values.get(symbol, 0.0)

    async def reset(self, symbol: Optional[str] = None) -> None:
        """Сбросить CVD.

        - reset("BTCUSDT") - только указанный символ
        - reset() или reset(None) - все символы
        """
        async with self._lock:
            if symbol is None:
                self._values.clear()
                logger.info("CVDTracker: reset all symbols")
            else:
                self._values.pop(symbol, None)
                logger.info("CVDTracker: reset %s", symbol)

    async def get_all(self) -> dict[str, float]:
        """Снимок всех CVD. Возвращает копию (защита от внешних мутаций)."""
        async with self._lock:
            return dict(self._values)