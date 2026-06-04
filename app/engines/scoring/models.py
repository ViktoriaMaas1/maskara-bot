"""
Scoring Engine models (Stage 11).
ScoreComponent — один источник баллов. ScoringResult — агрегированный вердикт.
Веса из спеки проекта (TradingView 20, Liquidity 20, Delta 15, Imbalance 15,
Volume 10, Trend 10, OI 10, News 10, Social 10).
"""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field

# Веса источников (из спеки)
W_TRADINGVIEW = 20
W_LIQUIDITY = 20
W_DELTA = 15
W_IMBALANCE = 15
W_VOLUME = 10
W_TREND = 10
W_OI = 10
W_NEWS = 10
W_SOCIAL = 10

LONG = "LONG"
SHORT = "SHORT"


class ScoreComponent(BaseModel):
    """Один источник баллов."""
    name: str
    weight: int
    available: bool
    points: float = 0.0
    direction: Optional[str] = None  # LONG / SHORT / None
    note: str = ""


class ScoringResult(BaseModel):
    """Результат скоринга для одного символа."""
    symbol: str
    decision: str = "NO_TRADE"        # TRADE / NO_TRADE
    direction: Optional[str] = None   # LONG / SHORT / None
    final_score: float = 0.0          # 0..100, нормализован по доступным источникам
    confidence: str = "LOW"           # LOW / MEDIUM / HIGH
    position_size: str = "none"       # none / small / normal
    components: List[ScoreComponent] = Field(default_factory=list)
    reason: List[str] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
