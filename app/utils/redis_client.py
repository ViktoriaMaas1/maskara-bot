"""
Redis клиент — единая точка подключения.

Используется для:
- Rate limit (Шаг 1.7)
- Дедупликация webhook (Шаг 1.7)
- Real-Time Market Cache (Stage 6)
- Кеш AI решений (Stage 10)

Архитектура:
- Singleton — один пул соединений на всё приложение
- redis.asyncio — асинхронный клиент (FastAPI же async)
- Подключение создаётся в lifespan(), закрывается при остановке
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

# Глобальный экземпляр — заполняется в lifespan()
_redis_client: Optional[aioredis.Redis] = None


async def init_redis() -> aioredis.Redis:
    """
    Создаёт пул соединений и проверяет доступность.

    Вызывается из main.py:lifespan() при старте приложения.
    Если Redis недоступен — приложение падает на старте (это правильно:
    лучше упасть сразу чем работать частично сломанным).
    """
    global _redis_client

    settings = get_settings()
    client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        # Пул соединений — несколько одновременных запросов не блокируют друг друга
        max_connections=20,
        # Таймауты — не зависаем если Redis тормозит
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
        health_check_interval=30,
    )

    # Проверяем что реально работает
    try:
        await client.ping()
    except Exception as e:
        logger.error("Redis недоступен: %s", e)
        await client.aclose()
        raise

    _redis_client = client
    logger.info(
        "Redis подключён",
        extra={"host": settings.redis_host, "port": settings.redis_port},
    )
    return client


async def close_redis() -> None:
    """Закрывает соединение. Вызывается в lifespan() при остановке."""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
        logger.info("Redis соединение закрыто")


def get_redis() -> aioredis.Redis:
    """
    Получить уже инициализированный клиент.

    Используется как FastAPI dependency:
        from fastapi import Depends
        from app.utils.redis_client import get_redis

        async def my_endpoint(redis = Depends(get_redis)):
            await redis.set("key", "value")
    """
    if _redis_client is None:
        raise RuntimeError(
            "Redis не инициализирован. "
            "Это значит lifespan() не отработал — проверь main.py"
        )
    return _redis_client
