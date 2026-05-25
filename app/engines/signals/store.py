"""SignalStore — репозиторий для таблицы signals (Postgres).

Сохраняет сигналы, выданные SignalGenerator (Stage 8),
читает недавние сигналы для API endpoint /signals/recent,
удаляет старые по retention.

Стиль — TradeRepository (app/database/trade_repository.py).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import SignalRow
from app.engines.signals.models import Signal

logger = logging.getLogger(__name__)


class SignalStore:
    """Репозиторий для сигналов Signal Generator.

    Использование:
        async with sm() as session:
            store = SignalStore(session)
            row = await store.save(signal)
            await session.commit()
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ============================================================
    # Запись
    # ============================================================

    async def save(self, signal: Signal) -> SignalRow:
        """Сохранить сигнал в БД.

        Возвращает созданный SignalRow с заполненным id.
        Caller должен вызвать session.commit() сам.
        """
        row = SignalRow(
            id=uuid.uuid4(),
            symbol=signal.symbol,
            timestamp_ms=signal.timestamp_ms,
            action=signal.action.value,
            strength=signal.strength.value,
            score=signal.score,
            reasons=list(signal.reasons),
            snapshot=dict(signal.snapshot),
            note=signal.note,
        )
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)

        logger.info(
            "Signal сохранён",
            extra={
                "signal_id": str(row.id),
                "symbol": row.symbol,
                "action": row.action,
                "strength": row.strength,
                "score": float(row.score),
            },
        )
        return row

    # ============================================================
    # Чтение
    # ============================================================

    async def get_recent(
        self,
        symbol: Optional[str] = None,
        limit: int = 50,
    ) -> list[SignalRow]:
        """Получить недавние сигналы.

        Args:
            symbol: если задан, фильтруем по символу. Иначе — все символы.
            limit: максимальное число записей.

        Возвращает: список SignalRow, отсортированный по created_at DESC.
        """
        stmt = select(SignalRow).order_by(SignalRow.created_at.desc()).limit(limit)
        if symbol is not None:
            stmt = stmt.where(SignalRow.symbol == symbol)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, signal_id: uuid.UUID) -> Optional[SignalRow]:
        """Найти сигнал по id. None если не найден."""
        stmt = select(SignalRow).where(SignalRow.id == signal_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # ============================================================
    # Cleanup (retention)
    # ============================================================

    async def cleanup_old(self, days: int = 30) -> int:
        """Удалить сигналы старше N дней.

        Используется фоновой задачей retention.
        Возвращает: число удалённых записей.
        Caller должен вызвать session.commit() сам.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        stmt = delete(SignalRow).where(SignalRow.created_at < cutoff)
        result = await self.session.execute(stmt)
        deleted = result.rowcount or 0

        logger.info(
            "Очистка старых сигналов",
            extra={"days": days, "deleted": deleted, "cutoff": cutoff.isoformat()},
        )
        return deleted

    async def count_all(self) -> int:
        """Общее число сигналов в таблице (для метрик/тестов)."""
        from sqlalchemy import func

        stmt = select(func.count()).select_from(SignalRow)
        result = await self.session.execute(stmt)
        return int(result.scalar_one())