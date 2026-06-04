from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ============================================================
# Enums
# ============================================================

class SignalAction(str, Enum):
    """Направление торгового сигнала."""

    BUY = "BUY"
    SELL = "SELL"


class SignalStrength(str, Enum):
    """Сила сигнала — определяется числом сработавших правил.

    WEAK   — 1 правило сработало
    MEDIUM — 2 правила сработали
    STRONG — 3+ правил сработали
    """

    WEAK = "WEAK"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


# ============================================================
# Signal
# ============================================================

class Signal(BaseModel):
    """Торговый сигнал, сгенерированный SignalGenerator на основе
    OrderFlowSnapshot и набора правил."""

    symbol: str = Field(..., description="Торговый символ, например BTCUSDT")
    timestamp_ms: int = Field(
        ..., description="Unix timestamp в миллисекундах на момент генерации сигнала"
    )

    # Направление и сила
    action: SignalAction = Field(..., description="BUY или SELL")
    strength: SignalStrength = Field(
        ..., description="WEAK / MEDIUM / STRONG — по числу сработавших правил"
    )

    # Численная оценка [0.0 .. 1.0] — нормализованная сила
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Числовой score сигнала в диапазоне [0.0, 1.0]",
    )

    # Какие правила сработали (для логирования и отладки)
    reasons: List[str] = Field(
        default_factory=list,
        description="Имена сработавших правил, например ['orderbook_imbalance', 'cvd_trend']",
    )

    # Snapshot Order Flow метрик на момент сигнала (для аудита)
    snapshot: Dict[str, Any] = Field(
        default_factory=dict,
        description="Слепок ключевых метрик OrderFlowSnapshot на момент генерации",
    )

    # Опциональный комментарий (для будущего расширения)
    note: Optional[str] = Field(
        None, description="Необязательное человекочитаемое описание сигнала"
    )

    # ----- News influence (Stage 10 Phase 3) -----
    # Aggregated news mood [-1..1] at signal time, or None if unavailable.
    news_mood: Optional[float] = Field(
        None, description="Средний sentiment свежих новостей [-1..1] на момент сигнала"
    )
    # Score delta applied by news (after - before), 0.0 if none.
    news_score_adjustment: Optional[float] = Field(
        None, description="Насколько новости скорректировали score (дельта)"
    )