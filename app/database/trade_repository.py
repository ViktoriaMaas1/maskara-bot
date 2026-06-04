"""
Repository pattern — слой между ORM и бизнес-логикой.

Stage 3: реализованы 3 read-only метода для Risk Manager.
Stage 4 (сейчас): добавлены write-методы create_open_trade, close_trade,
                  get_open_trade_by_id — нужны Execution Engine для
                  замыкания цикла "открыли позицию → записали в БД →
                  Risk Manager видит её при следующей проверке".
Stage 11 (планируется): get_last_n_trades, market snapshots, decisions.

Зачем repository а не запросы в endpoint'ах:
- Можно мокать в тестах (вместо реальной БД)
- Бизнес-логика отделена от технологии хранения
- Если когда-то заменим Postgres → удобство замены
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Trade, AiDecision


class TradeRepository:
    """Read-only методы для Risk Manager (Stage 3) + write-методы (Stage 4)."""

    def __init__(self, session: AsyncSession):
        self.session = session

    # ============================================================
    # READ-ONLY методы (Stage 3) — для Risk Manager
    # ============================================================

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

    # ============================================================
    # WRITE методы (Stage 4) — для Execution Engine
    # ============================================================

    async def create_open_trade(
        self,
        *,
        symbol: str,
        side: str,                          # "Buy" / "Sell" (Bybit формат)
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        position_size: Decimal,
        leverage: int,
        bybit_order_id: Optional[str] = None,
        signal_id: Optional[uuid.UUID] = None,
        ai_decision_id: Optional[uuid.UUID] = None,
        timeframe: Optional[str] = None,
        risk_percent: Optional[Decimal] = None,
        final_score: Optional[int] = None,
        scores_breakdown: Optional[dict] = None,
        entry_reason: Optional[str] = None,
    ) -> Trade:
        """
        Создать запись об открытой сделке.

        Вызывается ExecutionEngine.open_position() СРАЗУ после успешного
        подтверждения от Bybit (place_market_order вернул retCode=0).

        Минимально обязательные поля: symbol, side, entry_price, stop_loss,
        take_profit, position_size, leverage. Остальное — опционально и
        заполняется по мере доступности (на Stage 4 многие поля будут None).

        Returns:
            Свежий Trade с id и created_at, уже закоммиченный в БД.
        """
        trade = Trade(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            leverage=leverage,
            status="open",
            bybit_order_id=bybit_order_id,
            signal_id=signal_id,
            ai_decision_id=ai_decision_id,
            timeframe=timeframe,
            risk_percent=risk_percent,
            final_score=final_score,
            scores_breakdown=scores_breakdown,
            entry_reason=entry_reason,
        )
        self.session.add(trade)
        await self.session.commit()
        await self.session.refresh(trade)
        return trade

    async def get_open_trade_by_id(self, trade_id: uuid.UUID) -> Optional[Trade]:
        """
        Найти открытую сделку по UUID.

        Возвращает None если:
        - сделка не найдена
        - сделка уже закрыта (status != "open")

        Используется ExecutionEngine.close_position() для поиска
        перед обновлением.
        """
        stmt = select(Trade).where(
            Trade.id == trade_id,
            Trade.status == "open",
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def close_trade(
        self,
        *,
        trade_id: uuid.UUID,
        exit_price: Decimal,
        pnl: Decimal,
        fees: Optional[Decimal] = None,
        slippage: Optional[Decimal] = None,
        exit_reason: Optional[str] = None,
    ) -> Optional[Trade]:
        """
        Закрыть открытую сделку — обновить поля результата.

        Автоматически выставляет:
        - status = "closed"
        - closed_at = текущее время UTC
        - result = "win" / "loss" / "breakeven" в зависимости от pnl
        - duration_sec = разница между created_at и closed_at

        Returns:
            Обновлённый Trade или None если сделка не найдена / уже закрыта.
        """
        trade = await self.get_open_trade_by_id(trade_id)
        if trade is None:
            return None

        now = datetime.now(timezone.utc)

        # Determine result
        if pnl > 0:
            result = "win"
        elif pnl < 0:
            result = "loss"
        else:
            result = "breakeven"

        # Duration в секундах
        if trade.created_at is not None:
            # created_at — timezone-aware (server_default=now())
            duration_sec = int((now - trade.created_at).total_seconds())
        else:
            duration_sec = None

        trade.exit_price = exit_price
        trade.pnl = pnl
        trade.fees = fees
        trade.slippage = slippage
        trade.exit_reason = exit_reason
        trade.status = "closed"
        trade.closed_at = now
        trade.result = result
        trade.duration_sec = duration_sec

        await self.session.commit()
        await self.session.refresh(trade)
        return trade

    # ============================================================
    # AI DECISION методы (Stage 11) — журнал решений
    # ============================================================

    async def save_ai_decision(
        self,
        *,
        symbol: str,
        decision: str,
        direction: Optional[str],
        confidence: Optional[str],
        final_score: Optional[float],
        components: list,
        full_response: dict,
        signal_id: Optional[uuid.UUID] = None,
    ) -> "AiDecision":
        """Записать решение AI Decision Engine в журнал (ai_decisions).

        Stage 11: пишем только TRADE-решения (компактный журнал намерений
        войти). Маппит компоненты scoring в score-колонки, полный вердикт
        кладёт в full_response (JSONB). signal_id=None для проактивных
        решений (без TradingView).

        Returns: закоммиченный AiDecision с id и created_at.
        """
        # извлекаем баллы по компонентам в соответствующие колонки
        by_name = {c.get("name"): c for c in (full_response.get("components") or [])}

        def pts(name: str) -> Optional[int]:
            c = by_name.get(name)
            if not c or not c.get("available"):
                return None
            return int(round(c.get("points", 0)))

        # orderflow_score = сумма delta+imbalance+volume (если доступны)
        of_parts = [pts("delta"), pts("imbalance"), pts("volume")]
        of_avail = [p for p in of_parts if p is not None]
        orderflow_score = sum(of_avail) if of_avail else None

        row = AiDecision(
            signal_id=signal_id,
            decision=decision,
            direction=direction,
            confidence=confidence,
            final_score=int(round(final_score)) if final_score is not None else None,
            liquidity_score=pts("liquidity"),
            orderflow_score=orderflow_score,
            news_score=pts("news"),
            social_score=pts("social"),
            trend_score=pts("trend"),
            full_response=full_response,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def get_recent_ai_decisions(self, limit: int = 20) -> list:
        """Последние N решений AI (для дашборда/истории)."""
        stmt = (
            select(AiDecision)
            .order_by(AiDecision.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

