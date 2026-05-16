"""
API endpoints для Order Flow Engine.

Эти endpoint - read-only обёртки над OrderFlowEngine. Они НЕ обновляют CVD
(это делает periodic-job в Stage 8). Каждый вызов возвращает свежий snapshot
из MarketCache.

Маршруты:
  GET  /order-flow/{symbol}        - полный snapshot метрик
  GET  /order-flow/cvd/all          - все CVD одним списком
  POST /order-flow/cvd/reset/{symbol} - сбросить CVD (для отладки)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, status
from fastapi.responses import JSONResponse

from app.engines.order_flow.engine import (
    OrderFlowEngineNotInitialized,
    get_order_flow_engine,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/order-flow", tags=["order-flow"])


# ============================================================
# GET /order-flow/{symbol} - полный snapshot
# ============================================================

@router.get("/{symbol}", summary="Order Flow snapshot для символа")
async def get_order_flow_snapshot(
    symbol: str = Path(..., min_length=3, max_length=20, description="Символ, напр. BTCUSDT"),
) -> JSONResponse:
    """Текущий snapshot метрик order flow для символа.

    Возвращает delta/TFI/OBI/aggression на нескольких окнах + CVD.
    Если в Redis нет данных, data_available=False (метрики = 0).
    HTTP 200 в обоих случаях - фронтенд сам решает, что показывать.
    """
    try:
        engine = get_order_flow_engine()
    except OrderFlowEngineNotInitialized:
        logger.exception("OrderFlowEngine not initialized")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "engine_not_initialized"},
        )

    try:
        snapshot = await engine.get_snapshot(symbol)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=snapshot.model_dump(),
        )
    except Exception as e:
        logger.exception("Order flow snapshot failed for %s", symbol)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )


# ============================================================
# GET /order-flow/cvd/all - все CVD
# ============================================================

@router.get("/cvd/all", summary="Все накопленные CVD")
async def get_all_cvd() -> JSONResponse:
    """Текущие значения CVD для всех символов, по которым шло обновление."""
    try:
        engine = get_order_flow_engine()
    except OrderFlowEngineNotInitialized:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "engine_not_initialized"},
        )

    try:
        all_cvd = await engine.get_all_cvd()
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"cvd": all_cvd, "count": len(all_cvd)},
        )
    except Exception as e:
        logger.exception("get_all_cvd failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )


# ============================================================
# POST /order-flow/cvd/reset/{symbol} - сброс CVD (отладка)
# ============================================================

@router.post("/cvd/reset/{symbol}", summary="Сбросить CVD символа")
async def reset_cvd(
    symbol: str = Path(..., min_length=3, max_length=20),
) -> JSONResponse:
    """Сбросить накопленный CVD для символа. Используется для отладки."""
    try:
        engine = get_order_flow_engine()
    except OrderFlowEngineNotInitialized:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "engine_not_initialized"},
        )

    try:
        await engine.reset_cvd(symbol)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ok", "reset": symbol.upper()},
        )
    except Exception as e:
        logger.exception("reset_cvd failed for %s", symbol)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )