"""Тесты SignalStore — работа с таблицей signals в Postgres.

Тесты используют реальную БД через async_session фикстуру.
Каждый тест откатывает транзакцию (rollback) — данные не сохраняются.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.database.db import get_sessionmaker, init_db
from app.database.models import SignalRow
from app.engines.signals.models import Signal, SignalAction, SignalStrength
from app.engines.signals.store import SignalStore


# ============================================================
# Фикстуры
# ============================================================

@pytest_asyncio.fixture
async def async_session():
    """Открыть AsyncSession, откатить транзакцию после теста.

    Каждый тест получает чистую сессию.
    rollback() гарантирует, что тестовые данные не остаются в БД.
    """
    await init_db()
    sm = get_sessionmaker()
    async with sm() as session:
        try:
            yield session
        finally:
            await session.rollback()


def _make_signal(
    symbol: str = "TEST_BTCUSDT",
    action: SignalAction = SignalAction.BUY,
    strength: SignalStrength = SignalStrength.MEDIUM,
    score: float = 0.5,
    timestamp_ms: int = 1_700_000_000_000,
) -> Signal:
    """Фабрика тестовых сигналов."""
    return Signal(
        symbol=symbol,
        timestamp_ms=timestamp_ms,
        action=action,
        strength=strength,
        score=score,
        reasons=["test_rule"],
        snapshot={"obi_top10": 0.8, "cvd": 1.5},
        note="unit test",
    )


# ============================================================
# Тесты
# ============================================================

@pytest.mark.asyncio
async def test_save_signal(async_session):
    """save() создаёт запись с корректными полями и возвращает SignalRow."""
    store = SignalStore(async_session)
    signal = _make_signal(symbol=f"T_SAV_{uuid.uuid4().hex[:8]}")

    row = await store.save(signal)

    assert isinstance(row, SignalRow)
    assert row.id is not None
    assert row.symbol == signal.symbol
    assert row.action == "BUY"
    assert row.strength == "MEDIUM"
    assert float(row.score) == 0.5
    assert row.reasons == ["test_rule"]
    assert row.snapshot == {"obi_top10": 0.8, "cvd": 1.5}
    assert row.note == "unit test"
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_get_recent_filtered_by_symbol(async_session):
    """get_recent(symbol=X) возвращает только сигналы с этим символом."""
    store = SignalStore(async_session)
    unique_symbol = f"T_FLT_{uuid.uuid4().hex[:8]}"

    # Создаём 2 BTCUSDT и 1 ETHUSDT (с уникальными prefixe)
    await store.save(_make_signal(symbol=unique_symbol))
    await store.save(_make_signal(symbol=unique_symbol, action=SignalAction.SELL))
    await store.save(_make_signal(symbol=f"OTH_{uuid.uuid4().hex[:8]}"))
    await async_session.flush()

    rows = await store.get_recent(symbol=unique_symbol, limit=10)

    assert len(rows) == 2
    assert all(r.symbol == unique_symbol for r in rows)


@pytest.mark.asyncio
async def test_get_recent_returns_desc_order(async_session):
    """get_recent() сортирует по created_at DESC (свежие первыми)."""
    store = SignalStore(async_session)
    unique_symbol = f"T_ORD_{uuid.uuid4().hex[:8]}"

    # Сохраняем 3 сигнала с искусственно разнесённым created_at,
    # чтобы порядок был детерминирован (без зависимости от мс).
    base = datetime.now(timezone.utc)

    sig_old = await store.save(_make_signal(symbol=unique_symbol, score=0.25))
    sig_mid = await store.save(_make_signal(symbol=unique_symbol, score=0.50))
    sig_new = await store.save(_make_signal(symbol=unique_symbol, score=0.75))
    await async_session.flush()

    sig_old.created_at = base - timedelta(seconds=20)
    sig_mid.created_at = base - timedelta(seconds=10)
    sig_new.created_at = base
    await async_session.flush()

    rows = await store.get_recent(symbol=unique_symbol, limit=10)

    assert len(rows) == 3
    # Свежий первый, старый последний — порядок детерминирован.
    assert rows[0].id == sig_new.id
    assert rows[1].id == sig_mid.id
    assert rows[2].id == sig_old.id

@pytest.mark.asyncio
async def test_get_by_id(async_session):
    """get_by_id() возвращает сигнал; None для несуществующего id."""
    store = SignalStore(async_session)
    unique_symbol = f"T_BID_{uuid.uuid4().hex[:8]}"

    saved = await store.save(_make_signal(symbol=unique_symbol))
    await async_session.flush()

    # Существующий
    found = await store.get_by_id(saved.id)
    assert found is not None
    assert found.id == saved.id
    assert found.symbol == unique_symbol

    # Несуществующий
    missing = await store.get_by_id(uuid.uuid4())
    assert missing is None


@pytest.mark.asyncio
async def test_cleanup_old_deletes_only_old(async_session):
    """cleanup_old(days=N) удаляет только сигналы старше N дней."""
    store = SignalStore(async_session)
    unique_symbol = f"T_CLN_{uuid.uuid4().hex[:8]}"

    # Сохраняем 2 сигнала — оба свежие (created_at = now)
    fresh1 = await store.save(_make_signal(symbol=unique_symbol))
    fresh2 = await store.save(_make_signal(symbol=unique_symbol))
    await async_session.flush()

    # Искусственно состарим один из них на 40 дней
    fresh1.created_at = datetime.now(timezone.utc) - timedelta(days=40)
    await async_session.flush()

    # Cleanup с retention 30 дней
    deleted = await store.cleanup_old(days=30)

    # Удалить должно как минимум 1 (наш состаренный)
    assert deleted >= 1

    # Свежий должен остаться
    remaining = await store.get_by_id(fresh2.id)
    assert remaining is not None

    # Состаренный должен исчезнуть
    gone = await store.get_by_id(fresh1.id)
    assert gone is None