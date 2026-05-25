"""SignalWorker — фоновая asyncio задача Signal Generator.

Каждые SIGNAL_POLLING_INTERVAL_SEC секунд:
    1. Для каждого символа из SIGNAL_SYMBOLS:
       a. Берёт OrderFlowSnapshot из OrderFlowEngine
       b. Передаёт его в SignalGenerator.process_snapshot()
       c. Логирует результат
    2. Спит до следующего тика.

Один сбой в одной итерации не убивает loop — ошибка логируется,
worker идёт в следующий тик.

Использование (в main.py lifespan):
    worker = SignalWorker(generator, of_engine, symbols, interval_sec)
    await worker.start()  # запускает asyncio.create_task
    ...
    await worker.stop()   # отменяет задачу
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Sequence

from app.engines.order_flow.engine import OrderFlowEngine
from app.engines.signals.generator import SignalGenerator

logger = logging.getLogger(__name__)


class SignalWorker:
    """Фоновый цикл: snapshot → generator → log → sleep."""

    def __init__(
        self,
        generator: SignalGenerator,
        order_flow_engine: OrderFlowEngine,
        symbols: Sequence[str],
        interval_sec: int = 5,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError(f"interval_sec must be > 0, got {interval_sec}")
        if not symbols:
            raise ValueError("symbols must not be empty")

        self._generator = generator
        self._engine = order_flow_engine
        self._symbols = list(symbols)
        self._interval = interval_sec

        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    # ============================================================
    # Управление жизненным циклом
    # ============================================================

    async def start(self) -> None:
        """Запустить фоновый loop. Идемпотентно — повторный start ничего не делает."""
        if self._task is not None and not self._task.done():
            logger.warning("SignalWorker уже запущен")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="signal_worker")
        logger.info(
            "SignalWorker запущен",
            extra={
                "symbols": self._symbols,
                "interval_sec": self._interval,
            },
        )

    async def stop(self) -> None:
        """Корректно остановить worker — посылает stop_event, ждёт завершения task."""
        if self._task is None:
            return

        self._stop_event.set()
        try:
            # Даём worker завершить текущую итерацию (макс interval + 2 сек)
            await asyncio.wait_for(self._task, timeout=self._interval + 2)
        except asyncio.TimeoutError:
            logger.warning("SignalWorker не завершился вовремя — отменяем")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
            logger.info("SignalWorker остановлен")

    # ============================================================
    # Внутреннее
    # ============================================================

    async def _run(self) -> None:
        """Основной цикл. Работает пока не выставлен stop_event."""
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                # Корректная отмена — выходим без логирования как ошибки
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "SignalWorker tick упал с исключением — продолжаем",
                    extra={"error": str(e)},
                )

            # Сон с поддержкой быстрого выхода через stop_event
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
                # Если ждать получилось — значит stop_event сработал, выходим
                break
            except asyncio.TimeoutError:
                # Нормальный таймаут — идём в следующий тик
                continue

    async def _tick(self) -> None:
        """Один тик: для каждого символа получить snapshot и прогнать через generator."""
        for symbol in self._symbols:
            try:
                snapshot = await self._engine.get_snapshot(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Не удалось получить OrderFlowSnapshot — пропускаем символ",
                    extra={"symbol": symbol, "error": str(e)},
                )
                continue

            if not snapshot.data_available:
                logger.debug(
                    "Нет данных для символа — пропускаем",
                    extra={"symbol": symbol},
                )
                continue

            try:
                signal = await self._generator.process_snapshot(snapshot)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "SignalGenerator упал на snapshot — пропускаем",
                    extra={"symbol": symbol, "error": str(e)},
                )
                continue

            if signal is not None:
                logger.info(
                    "Сигнал сгенерирован worker'ом",
                    extra={
                        "symbol": signal.symbol,
                        "action": signal.action.value,
                        "strength": signal.strength.value,
                        "score": signal.score,
                    },
                )


# ============================================================
# Singleton helper'ы (для main.py lifespan)
# ============================================================

_signal_worker: Optional[SignalWorker] = None


def init_signal_worker(
    generator: SignalGenerator,
    order_flow_engine: OrderFlowEngine,
    symbols: Sequence[str],
    interval_sec: int = 5,
) -> SignalWorker:
    """Создать и запомнить SignalWorker singleton. Вызывается в lifespan."""
    global _signal_worker
    if _signal_worker is not None:
        logger.warning("SignalWorker уже инициализирован")
        return _signal_worker
    _signal_worker = SignalWorker(
        generator=generator,
        order_flow_engine=order_flow_engine,
        symbols=symbols,
        interval_sec=interval_sec,
    )
    return _signal_worker


def get_signal_worker() -> SignalWorker:
    """Получить singleton SignalWorker. Бросает если не инициализирован."""
    if _signal_worker is None:
        raise RuntimeError(
            "SignalWorker не инициализирован. Вызови init_signal_worker() в lifespan."
        )
    return _signal_worker


async def close_signal_worker() -> None:
    """Корректно остановить singleton. Вызывается при shutdown."""
    global _signal_worker
    if _signal_worker is None:
        return
    await _signal_worker.stop()
    _signal_worker = None