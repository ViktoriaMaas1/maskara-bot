"""
ORM модели — схема БД для всего проекта.

Заполнение моделей идёт по этапам:
- Stage 1 (сейчас):  схема создана, заполняется только webhook_signals
- Stage 11:          активно используются trades, market_snapshots, ai_decisions
- Stage 9:           news_events
- Stage 12:          ai_memory
- Stage 11:          strategy_versions

Принципы дизайна:
- Все таблицы имеют id (UUID) + created_at
- Финансовые числа = Numeric (НИКАКИХ float — потеря точности на больших суммах)
- JSONB для гибких полей (snapshot рынка, AI reasoning, scoring breakdown)
- Индексы на полях по которым будем фильтровать (symbol, created_at, status)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.db import Base


# ==============================================================
# 1. WEBHOOK SIGNALS — каждый сигнал от TradingView
# ==============================================================
class WebhookSignal(Base):
    """
    Каждый принятый сигнал от TradingView.
    Заполняется СРАЗУ в Stage 1 — даже до того как появится торговля.

    Это аудит: всегда можем сказать "когда что прилетело и что мы решили".
    """

    __tablename__ = "webhook_signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    # Из payload
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(8))            # BUY/SELL
    timeframe: Mapped[str] = mapped_column(String(8))       # 1m/3m/15m/1h
    strategy: Mapped[str] = mapped_column(String(64), index=True)
    support_zone: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    resistance_zone: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))

    # Метаданные
    client_ip: Mapped[Optional[str]] = mapped_column(String(45))
    status: Mapped[str] = mapped_column(String(20), index=True)  # accepted/duplicate/rejected
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)

    # Связь со сделкой (если этот сигнал привёл к открытию)
    trade_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trades.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_signals_symbol_created", "symbol", "created_at"),
    )


# ==============================================================
# 2. AI DECISIONS — что AI решил по каждому сигналу
# ==============================================================
class AiDecision(Base):
    """
    AI Decision Engine выдал JSON — сохраняем целиком + ключевые поля для запросов.
    Заполняется в Stage 10.
    """

    __tablename__ = "ai_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_signals.id", ondelete="CASCADE"), index=True
    )

    # Решение
    decision: Mapped[str] = mapped_column(String(16))         # TRADE/NO_TRADE
    direction: Mapped[Optional[str]] = mapped_column(String(8))  # LONG/SHORT
    probability: Mapped[Optional[int]] = mapped_column(Integer)
    confidence: Mapped[Optional[str]] = mapped_column(String(8))   # LOW/MEDIUM/HIGH
    risk_level: Mapped[Optional[str]] = mapped_column(String(8))

    # Scores
    market_score: Mapped[Optional[int]] = mapped_column(Integer)
    liquidity_score: Mapped[Optional[int]] = mapped_column(Integer)
    orderflow_score: Mapped[Optional[int]] = mapped_column(Integer)
    news_score: Mapped[Optional[int]] = mapped_column(Integer)
    social_score: Mapped[Optional[int]] = mapped_column(Integer)
    trend_score: Mapped[Optional[int]] = mapped_column(Integer)
    final_score: Mapped[Optional[int]] = mapped_column(Integer, index=True)

    # Полный AI ответ — JSONB чтобы хранить reason/warnings и любые расширения
    full_response: Mapped[dict] = mapped_column(JSONB)


# ==============================================================
# 3. MARKET SNAPSHOTS — состояние рынка на момент входа/выхода
# ==============================================================
class MarketSnapshot(Base):
    """
    Снимок рынка для последующего анализа AI.
    Без этого Self-Learning не будет работать — нечему учиться.

    Stage 5-6: WebSocket собирает данные → сохраняем сюда
    """

    __tablename__ = "market_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    symbol: Mapped[str] = mapped_column(String(20), index=True)
    snapshot_type: Mapped[str] = mapped_column(String(20))  # entry/exit/event_risk

    # Цены
    last_price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    bid: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    ask: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    spread: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))

    # Order flow
    delta_5m: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    cumulative_delta: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    volume_5m: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    orderbook_imbalance: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))

    # External data
    open_interest: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    funding_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6))
    long_short_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))

    # Полный снимок для расширения без миграций
    raw_data: Mapped[Optional[dict]] = mapped_column(JSONB)


# ==============================================================
# 4. NEWS EVENTS — зафиксированные новости
# ==============================================================
class NewsEvent(Base):
    """
    Зафиксированное новостное событие. Используется и как фильтр
    (Event Risk Mode), и для post-mortem анализа сделок.

    Заполняется в Stage 9.
    """

    __tablename__ = "news_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    source: Mapped[str] = mapped_column(String(64))           # cryptopanic/twitter/...
    category: Mapped[str] = mapped_column(String(32), index=True)  # crypto/macro/regulation/...
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[Optional[str]] = mapped_column(Text)

    # Sentiment / impact
    sentiment: Mapped[Optional[str]] = mapped_column(String(16))  # bullish/bearish/neutral
    impact_score: Mapped[Optional[int]] = mapped_column(Integer)
    fake_risk: Mapped[Optional[int]] = mapped_column(Integer)
    is_priced_in: Mapped[Optional[bool]] = mapped_column(Boolean)

    raw_data: Mapped[Optional[dict]] = mapped_column(JSONB)


# ==============================================================
# 5. TRADES — главная таблица. Журнал сделок.
# ==============================================================
class Trade(Base):
    """
    Каждая открытая/закрытая сделка. Источник правды для:
    - PnL отчётов
    - анализа выигрышных/убыточных паттернов (Stage 12)
    - Telegram /last_trades, /daily_pnl
    - dashboard'a

    Заполняется в Stage 11.
    """

    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)

    # Связи
    signal_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_signals.id"), nullable=True
    )
    ai_decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_decisions.id"), nullable=True
    )
    strategy_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=True
    )
    snapshot_entry_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("market_snapshots.id"), nullable=True
    )
    snapshot_exit_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("market_snapshots.id"), nullable=True
    )

    # Идентификация на бирже
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    bybit_order_id: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    bybit_position_idx: Mapped[Optional[int]] = mapped_column(Integer)

    # Параметры сделки
    side: Mapped[str] = mapped_column(String(8))                  # Buy/Sell
    timeframe: Mapped[Optional[str]] = mapped_column(String(8))
    leverage: Mapped[int] = mapped_column(Integer)
    position_size: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    risk_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))

    # Цены
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    take_profit: Mapped[Decimal] = mapped_column(Numeric(20, 8))

    # Результат
    status: Mapped[str] = mapped_column(String(16), index=True)   # open/closed/cancelled
    result: Mapped[Optional[str]] = mapped_column(String(16))     # win/loss/breakeven
    pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    fees: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    slippage: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    duration_sec: Mapped[Optional[int]] = mapped_column(BigInteger)

    # Качественный анализ
    entry_reason: Mapped[Optional[str]] = mapped_column(Text)
    exit_reason: Mapped[Optional[str]] = mapped_column(Text)
    mistake_category: Mapped[Optional[str]] = mapped_column(String(32))  # для Self-Learning

    # Полные scores на момент входа (snapshot)
    final_score: Mapped[Optional[int]] = mapped_column(Integer)
    scores_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB)

    __table_args__ = (
        Index("ix_trades_symbol_status", "symbol", "status"),
        Index("ix_trades_created_status", "created_at", "status"),
    )


# ==============================================================
# 6. AI MEMORY — паттерны для self-learning
# ==============================================================
class AiMemory(Base):
    """
    "Память" AI: какие паттерны работают, какие нет.
    Заполняется в Stage 12 после анализа закрытых сделок.

    Используется для предложений (НЕ автоматических изменений) — человек одобряет.
    """

    __tablename__ = "ai_memory"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    pattern_type: Mapped[str] = mapped_column(String(32), index=True)
    # profitable / losing / dangerous_market_condition / etc.

    pattern_key: Mapped[str] = mapped_column(String(128), index=True)
    pattern_data: Mapped[dict] = mapped_column(JSONB)

    # Статистика по паттерну
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    avg_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8))
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))


# ==============================================================
# 7. STRATEGY VERSIONS — контроль версий стратегии
# ==============================================================
class StrategyVersion(Base):
    """
    Каждое изменение стратегии. Из задания:
    "Every strategy change must save: version, changed rules, reason,
    backtest result, forward test result, approval, date/time"
    """

    __tablename__ = "strategy_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    version_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    changed_rules: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str] = mapped_column(Text)

    backtest_result: Mapped[Optional[dict]] = mapped_column(JSONB)
    forward_test_result: Mapped[Optional[dict]] = mapped_column(JSONB)

    approval_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/approved/rejected
    approved_by: Mapped[Optional[str]] = mapped_column(String(64))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
