"""
Repository pattern - слой между ORM и бизнес-логикой.

Stage 3: реализованы 3 read-only метода для Risk Manager.
Stage 11 (планируется): create_open_trade, close_trade, get_open_position,
                       get_last_n_trades.

Зачем repository а не запросы в endpoint'ах:
- Можно мокать в тестах (вместо реальной БД)
- Бизнес-логика отделена от технологии хранения
- Если когда-то заменим Postgres → удобство замены
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Trade


class TradeRepository:
    """Read-only методы для Risk Manager (Stage 3)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_daily_pnl(self) -> Decimal:
        """
        Сумма PnL по всем сделкам, закрытым за последние 24 часа.
        Возвращает Decimal (для денежных расчётов float НЕ используем).
        Если закрытых сделок нет — возвращает Decimal("0").
        """
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        stmt = select(func.coalesce(func.sum(Trade.pnl), 0)).where(
            Trade.closed_at >= since,
            Trade.pnl.is_not(None),
        )
        result = await self.session.execute(stmt)
        value = result.scalar()
        return Decimal(str(value)) if value is not None else Decimal("0")

    async def get_consecutive_losses(self, limit: int = 10) -> int:
        """
        Количество последних подряд идущих убыточных сделок.
        Смотрим только сделки с result IN ('win', 'loss') — игнорируем breakeven и cancelled.
        Идём с конца (от свежей к старой) и считаем 'loss' до первого 'win'.
        """
        stmt = (
            select(Trade.result)
            .where(Trade.result.in_(["win", "loss"]))
            .order_by(Trade.closed_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        rows = result.scalars().all()

        count = 0
        for r in rows:
            if r == "loss":
                count += 1
            else:
                break
        return count

    async def count_open_by_symbol(self, symbol: str) -> int:
        """
        Количество открытых сделок по символу.
        Открытая = closed_at IS NULL AND status = 'open'.
        """
        stmt = select(func.count()).select_from(Trade).where(
            Trade.symbol == symbol,
            Trade.closed_at.is_(None),
            Trade.status == "open",
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)
