from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ====================================================================
# Enums
# ====================================================================

class ZoneSide(str, Enum):
    """Расположение зоны ликвидности относительно текущей цены.

    ABOVE — зона выше текущей цены (ликвидность на стороне продавцов / asks)
    BELOW — зона ниже текущей цены (ликвидность на стороне покупателей / bids)
    """

    ABOVE = "ABOVE"
    BELOW = "BELOW"


# ====================================================================
# LiquidityZone — одна зона ликвидности (крупная стенка в стакане)
# ====================================================================

class LiquidityZone(BaseModel):
    """Зона ликвидности — уровень цены с крупным объёмом лимитных заявок.

    Определяется из orderbook: уровни, где размер заявки заметно
    превышает средний по стакану (потенциальная "стенка" / магнит цены).
    """

    price: float = Field(..., description="Цена уровня")
    size: float = Field(..., description="Объём заявок на этом уровне")
    side: ZoneSide = Field(..., description="ABOVE или BELOW относительно цены")
    distance_pct: float = Field(
        ..., description="Расстояние от текущей цены в процентах (по модулю)"
    )


# ====================================================================
# LiquiditySnapshot — снимок состояния ликвидности по символу
# ====================================================================

class LiquiditySnapshot(BaseModel):
    """Снимок ликвидности для символа на текущий момент.

    Если данных в кэше нет (стакан пуст) — data_available=False,
    остальные поля заполняются дефолтами. Эндпоинт всегда отдаёт HTTP 200,
    фронт сам решает, что показывать (как в order_flow).
    """

    symbol: str = Field(..., description="Торговый символ, например BTCUSDT")
    timestamp_ms: int = Field(..., description="Unix timestamp в миллисекундах")
    data_available: bool = Field(
        default=False, description="False если в кэше нет orderbook"
    )

    mid_price: float = Field(default=0.0, description="Средняя цена (bid+ask)/2")

    # Зоны ликвидности (крупные стенки), отсортированы по близости к цене
    zones_above: List[LiquidityZone] = Field(
        default_factory=list, description="Зоны ликвидности выше цены (asks)"
    )
    zones_below: List[LiquidityZone] = Field(
        default_factory=list, description="Зоны ликвидности ниже цены (bids)"
    )

    # Локальные экстремумы (из недавних сделок) — потенциальные sweep-уровни
    local_high: Optional[float] = Field(
        default=None, description="Локальный максимум цены за окно сделок"
    )
    local_low: Optional[float] = Field(
        default=None, description="Локальный минимум цены за окно сделок"
    )

    # Ликвидации
    recent_liquidation_count: int = Field(
        default=0, description="Число недавних ликвидаций в кэше"
    )
