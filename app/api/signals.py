"""REST endpoints для Signal Generator (Stage 8).

GET /signals/recent          — последние сигналы из БД
GET /signals/recent/{symbol} — последние сигналы по символу
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Path, Query, status
from fastapi.responses import JSONResponse

from app.database.db import get_sessionmaker
from app.engines.signals.store import SignalStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])


# ============================================================
# Helpers
# ============================================================

def _row_to_dict(row) -> dict:
    """Конвертирует SignalRow → JSON-сериализуемый dict."""
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "symbol": row.symbol,
        "timestamp_ms": row.timestamp_ms,
        "action": row.action,
        "strength": row.strength,
        "score": float(row.score),
        "reasons": row.reasons or [],
        "snapshot": row.snapshot or {},
        "note": row.note,
    }


# ============================================================
# GET /signals/recent
# ============================================================

@router.get("/recent", summary="Последние сигналы")
async def get_recent_signals(
    symbol: Optional[str] = Query(
        None,
        min_length=3,
        max_length=20,
        description="Фильтр по символу, например BTCUSDT",
    ),
    limit: int = Query(
        50, ge=1, le=500, description="Максимальное число записей"
    ),
) -> JSONResponse:
    """Получить последние сигналы из БД (сортировка по created_at DESC).

    Без symbol — возвращает сигналы по всем символам.
    """
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            store = SignalStore(session)
            rows = await store.get_recent(symbol=symbol, limit=limit)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "count": len(rows),
                "symbol": symbol,
                "limit": limit,
                "signals": [_row_to_dict(r) for r in rows],
            },
        )
    except RuntimeError as e:
        # БД не инициализирована (lifespan ещё не отработал или упал)
        logger.warning("DB not ready for /signals/recent: %s", e)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "db_not_initialized"},
        )
    except Exception as e:
        logger.exception("/signals/recent failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )


# ============================================================
# GET /signals/recent/{symbol}
# ============================================================

@router.get("/recent/{symbol}", summary="Последние сигналы по символу")
async def get_recent_signals_by_symbol(
    symbol: str = Path(..., min_length=3, max_length=20),
    limit: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    """Получить последние сигналы для конкретного символа."""
    # Просто переиспользуем get_recent_signals — но в path-варианте symbol обязателен
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            store = SignalStore(session)
            rows = await store.get_recent(symbol=symbol.upper(), limit=limit)

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "count": len(rows),
                "symbol": symbol.upper(),
                "limit": limit,
                "signals": [_row_to_dict(r) for r in rows],
            },
        )
    except RuntimeError as e:
        logger.warning("DB not ready for /signals/recent/{symbol}: %s", e)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "db_not_initialized"},
        )
    except Exception as e:
        logger.exception("/signals/recent/%s failed", symbol)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )