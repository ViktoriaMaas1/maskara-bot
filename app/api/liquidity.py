"""
Liquidity API — эндпоинт для получения снимка ликвидности.

GET /liquidity/{symbol} → LiquiditySnapshot
"""

from fastapi import APIRouter, Path

from app.engines.liquidity.engine import get_liquidity_engine
from app.engines.liquidity.models import LiquiditySnapshot

router = APIRouter(prefix="/liquidity", tags=["liquidity"])


@router.get(
    "/{symbol}",
    summary="Liquidity snapshot для символа",
    response_model=LiquiditySnapshot,
)
async def get_liquidity_snapshot(
    symbol: str = Path(..., description="Торговый символ, например BTCUSDT")
) -> LiquiditySnapshot:
    """
    Получить снимок ликвидности для символа.

    Возвращает зоны ликвидности (стенки в стакане), локальные экстремумы,
    количество ликвидаций. Если данных нет — data_available=False.

    Args:
        symbol: торговый символ (BTCUSDT, ETHUSDT, и т.д.)

    Returns:
        LiquiditySnapshot с зонами, экстремумами, ликвидациями
    """
    engine = get_liquidity_engine()
    return await engine.get_snapshot(symbol)
