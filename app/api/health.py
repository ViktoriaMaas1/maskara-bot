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
from app.bybit.rest_client import BybitRestClient
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
        version="0.2.0-stage2",
    )


@router.get("/health/ready", summary="Readiness")
async def readiness(settings: SettingsDep) -> JSONResponse:
    """
    Readiness — бот готов принимать трафик?
    Проверяет связь с PostgreSQL, Redis и Bybit.
    """
    checks = {
        "postgres": await healthcheck_db(),
        "redis": False,
        "bybit": False,
    }

    # Проверка Redis
    try:
        redis = get_redis()
        await redis.ping()
        checks["redis"] = True
    except Exception as e:
        logger.warning("Redis healthcheck failed: %s", e)

    # Проверка Bybit (только если ключи заданы)
    if settings.bybit_api_key and settings.bybit_api_secret:
        try:
            bybit = BybitRestClient.from_settings(settings)
            checks["bybit"] = await bybit.health_check()
        except Exception as e:
            logger.warning("Bybit healthcheck failed: %s", e)
    else:
        # ключи не заданы — это норма для Stage 1, но в Stage 2+ должны быть
        logger.warning("Bybit keys not configured")

    all_ok = all(checks.values())
    payload = ReadinessResponse(
        status="ready" if all_ok else "not_ready",
        checks=checks,
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK if all_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
    )
