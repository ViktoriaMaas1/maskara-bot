"""
Pydantic-модели и константы для Order Flow Engine.

OrderFlowSnapshot — единый "слепок" метрик order flow на момент запроса.
Используется как ответ engine.get_snapshot() и как тело API endpoint.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# ============================================================
# Константы
# ============================================================

# Окна для скользящих метрик (delta, TFI, aggression), в секундах
WINDOWS_SECONDS = [30, 60, 300]

# Глубины стакана для OBI
OBI_DEPTHS = [5, 10, 20]

# Перцентиль для детекции крупных сделок ("китов")
LARGE_TRADE_PERCENTILE = 95.0

# Окно для подсчёта крупных сделок (секунды)
LARGE_TRADE_WINDOW_SEC = 60


# ============================================================
# Snapshot
# ============================================================

class OrderFlowSnapshot(BaseModel):
    """Снимок метрик order flow для одного символа на момент запроса."""

    symbol: str
    timestamp_ms: int = Field(..., description="Unix timestamp в миллисекундах")
    data_available: bool = Field(
        ..., description="False если в Redis нет свежих trades/orderbook"
    )

    # Дельта (buy_volume - sell_volume) за окно
    delta_30s: float = 0.0
    delta_1m: float = 0.0
    delta_5m: float = 0.0

    # CVD - накопленная дельта (in-memory per symbol)
    cvd: float = 0.0

    # OBI - Order Book Imbalance, [-1, +1]
    obi_top5: float = 0.0
    obi_top10: float = 0.0
    obi_top20: float = 0.0

    # TFI - Trade Flow Imbalance, [-1, +1]
    tfi_30s: float = 0.0
    tfi_1m: float = 0.0
    tfi_5m: float = 0.0

    # Aggression - доля покупок от общего объёма за 1м, [0, 1]
    buy_aggression_1m: float = 0.0

    # Общий объём за 1м (для контекста)
    total_volume_1m: float = 0.0

    # Кол-во крупных сделок (выше 95-го перцентиля) за 1м
    large_trade_count_1m: int = 0

    # Опциональные служебные поля
    trades_count_1m: int = 0
    orderbook_age_ms: Optional[int] = Field(
        None, description="Сколько мс прошло с момента обновления orderbook"
    )