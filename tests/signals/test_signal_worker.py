"""Тесты SignalWorker — фонового asyncio цикла.

Mock'аем OrderFlowEngine и SignalGenerator.
Используем короткий interval, чтобы тесты были быстрые.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.engines.order_flow.models import OrderFlowSnapshot
from app.engines.signals.models import Signal, SignalAction, SignalStrength
from app.workers.signal_worker import SignalWorker


# ============================================================
# Хелперы
# ============================================================

def _make_snapshot(symbol: str, data_available: bool = True) -> OrderFlowSnapshot:
    """Минимальный snapshot."""
    return OrderFlowSnapshot(
        symbol=symbol,
        timestamp_ms=1_700_000_000_000,
        data_available=data_available,
    )


def _make_signal(symbol: str) -> Signal:
    return Signal(
        symbol=symbol,
        timestamp_ms=1_700_000_000_000,
        action=SignalAction.BUY,
        strength=SignalStrength.STRONG,
        score=0.75,
        reasons=["test"],
    )


# ============================================================
# Тесты
# ============================================================

@pytest.mark.asyncio
async def test_worker_calls_generator_per_symbol():
    """Worker за один тик вызывает get_snapshot() и process_snapshot() для каждого символа."""
    symbols = ["BTCUSDT", "ETHUSDT"]

    # Mock OrderFlowEngine — возвращает snapshot для любого символа
    engine = MagicMock()
    engine.get_snapshot = MagicMock(
        side_effect=lambda sym: _make_snapshot(sym)
    )

    # Mock SignalGenerator — process_snapshot возвращает Signal
    generator = MagicMock()
    generator.process_snapshot = AsyncMock(
        side_effect=lambda snap: _make_signal(snap.symbol)
    )

    worker = SignalWorker(
        generator=generator,
        order_flow_engine=engine,
        symbols=symbols,
        interval_sec=0.05,  # короткий интервал — за 200-300мс пройдёт несколько тиков
    )

    await worker.start()
    # Даём один тик отработать
    await asyncio.sleep(0.2)
    await worker.stop()

    # get_snapshot должен быть вызван минимум по разу на каждый символ
    called_symbols = [c.args[0] for c in engine.get_snapshot.call_args_list]
    assert "BTCUSDT" in called_symbols
    assert "ETHUSDT" in called_symbols

    # process_snapshot тоже вызван минимум по разу на каждый символ
    assert generator.process_snapshot.await_count >= 2


@pytest.mark.asyncio
async def test_worker_continues_after_exception():
    """Если process_snapshot бросает исключение, worker продолжает работать."""
    symbols = ["BTCUSDT"]

    engine = MagicMock()
    engine.get_snapshot = MagicMock(side_effect=lambda sym: _make_snapshot(sym))

    # Первый вызов бросает, второй и далее — возвращают Signal
    generator = MagicMock()
    generator.process_snapshot = AsyncMock(
        side_effect=[RuntimeError("boom"), _make_signal("BTCUSDT"), _make_signal("BTCUSDT")]
    )

    worker = SignalWorker(
        generator=generator,
        order_flow_engine=engine,
        symbols=symbols,
        interval_sec=0.05,
    )

    await worker.start()
    # Даём успеть пройти 2-3 тика
    await asyncio.sleep(0.3)
    await worker.stop()

    # process_snapshot вызвана несколько раз — значит worker не умер от первого исключения
    assert generator.process_snapshot.await_count >= 2