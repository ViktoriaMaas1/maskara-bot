"""
Healthcheck endpoint.

GET /health — общий статус (для мониторинга / load balancer)
GET /health/ready — проверяет ВСЕ зависимости (для Kubernetes readiness probe)

Стандарт:
- 200 = всё ОК
- 503 = что-то сломано (бот не должен принимать трафик)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.dependencies import SettingsDep
from app.database.db import healthcheck_db
from app.utils.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    app_env: str
    bybit_testnet: bool
    version: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


@router.get("/health", response_model=HealthResponse, summary="Liveness")
async def healthcheck(settings: SettingsDep) -> HealthResponse:
    """
    Liveness — бот жив? (простая проверка)
    Используется Docker'ом и базовым мониторингом.
    """
    return HealthResponse(
        status="ok",
        app_env=settings.app_env.value,
        bybit_testnet=settings.bybit_testnet,
        version="0.1.0-stage1",
    )


@router.get("/health/ready", summary="Readiness")
async def readiness() -> JSONResponse:
    """
    Readiness — бот готов принимать трафик?
    Проверяет связь с PostgreSQL и Redis.
    """
    checks = {
        "postgres": await healthcheck_db(),
        "redis": False,
    }

    # Проверка Redis
    try:
        redis = get_redis()
        await redis.ping()
        checks["redis"] = True
    except Exception as e:
        logger.warning("Redis healthcheck failed: %s", e)

    all_ok = all(checks.values())
    payload = ReadinessResponse(
        status="ready" if all_ok else "not_ready",
        checks=checks,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
    )
