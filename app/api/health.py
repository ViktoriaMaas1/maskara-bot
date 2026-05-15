"""
Healthcheck endpoint.

GET /health — общий статус (для мониторинга / load balancer)
GET /health/ready — проверяет ВСЕ зависимости (для Kubernetes readiness probe)
GET /health/websocket — состояние Bybit WebSocket клиента (Stage 5)
GET /health/cache — состояние Real-Time Market Cache (Stage 6)

Стандарт:
- 200 = всё ОК
- 503 = что-то сломано (бот не должен принимать трафик)
"""

from __future__ import annotations

import logging
import time

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


@router.get("/health/websocket", summary="WebSocket health")
async def websocket_health(settings: SettingsDep) -> JSONResponse:
    """
    Состояние Bybit WebSocket клиента (Stage 5).

    Возвращает:
    - status: "disabled" | "healthy" | "degraded" | "unhealthy"
    - connected: соединение с Bybit активно
    - last_message_ts: unix timestamp последнего сообщения (0 если нет данных)
    - seconds_since_last_message: сколько секунд назад пришло последнее сообщение
    - symbols: на какие символы подписаны
    - failed_attempts: счётчик неудачных попыток подключения

    HTTP коды:
    - 200 = healthy ИЛИ disabled (фичефлаг выключен)
    - 503 = unhealthy / degraded / not_initialized
    """
    # Фичефлаг — WebSocket вообще выключен
    if not settings.bybit_ws_enabled:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "disabled"},
        )

    try:
        from app.bybit.websocket_client import get_websocket, WebSocketNotInitialized
        try:
            ws = get_websocket()
        except WebSocketNotInitialized:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "reason": "not_initialized"},
            )

        healthy = ws.is_healthy()
        last_ts = ws.last_message_ts
        since = (time.time() - last_ts) if last_ts > 0 else None

        if not ws.connected:
            ws_status = "unhealthy"
            http_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif healthy:
            ws_status = "healthy"
            http_code = status.HTTP_200_OK
        else:
            ws_status = "degraded"
            http_code = status.HTTP_503_SERVICE_UNAVAILABLE

        return JSONResponse(
            status_code=http_code,
            content={
                "status": ws_status,
                "connected": ws.connected,
                "last_message_ts": last_ts,
                "seconds_since_last_message": since,
                "symbols": settings.allowed_symbols_list,
                "failed_attempts": ws._failed_attempts,
            },
        )
    except Exception as e:
        logger.exception("WebSocket healthcheck failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "error": str(e)},
        )


@router.get("/health/cache", summary="Market Cache health")
async def cache_health(settings: SettingsDep) -> JSONResponse:
    """
    Состояние Real-Time Market Cache (Stage 6).

    Возвращает:
    - status: "disabled" | "healthy" | "unhealthy"
    - redis_ping: Redis отвечает на ping
    - stats: количество ключей по типам (orderbook/ticker/trades/klines/liquidations)
    - ttls: настройки TTL для каждого типа

    HTTP коды:
    - 200 = healthy ИЛИ disabled
    - 503 = unhealthy / not_initialized / redis_down
    """
    # Фичефлаг — кеш вообще выключен
    if not settings.market_cache_enabled:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "disabled"},
        )

    try:
        from app.cache.market_cache import (
            get_market_cache,
            MarketCacheNotInitialized,
        )
        try:
            cache = get_market_cache()
        except MarketCacheNotInitialized:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "unhealthy", "reason": "not_initialized"},
            )

        # Redis ping
        redis_ok = await cache.is_healthy()

        # Stats — сколько ключей по каждому типу
        stats = await cache.get_stats()

        # TTL настройки — для информации
        ttls = {
            "orderbook": settings.market_cache_orderbook_ttl_sec,
            "ticker": settings.market_cache_ticker_ttl_sec,
            "trades": settings.market_cache_trades_ttl_sec,
            "klines": settings.market_cache_klines_ttl_sec,
            "liquidations": settings.market_cache_liquidations_ttl_sec,
        }

        cache_status = "healthy" if redis_ok else "unhealthy"
        http_code = (
            status.HTTP_200_OK if redis_ok else status.HTTP_503_SERVICE_UNAVAILABLE
        )

        return JSONResponse(
            status_code=http_code,
            content={
                "status": cache_status,
                "redis_ping": redis_ok,
                "stats": stats,
                "ttls": ttls,
            },
        )
    except Exception as e:
        logger.exception("Cache healthcheck failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "error": str(e)},
        )