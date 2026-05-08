"""
Защита webhook через Redis.

Включает:

1. Rate limiting (по IP)
   Алгоритм: sliding window через Redis sorted set.
   Если IP превысил лимит — 429 Too Many Requests.

2. Дедупликация (по содержимому сигнала)
   Если такой же сигнал прилетел < N секунд назад — игнорируем.
   Защита от:
   - двойного срабатывания TradingView alert
   - сетевых retry от прокси
   - replay-атак (атакующий записал и переотправил)

Зачем именно Redis (а не in-memory dict):
- Переживает рестарты приложения
- Если запустим несколько worker'ов / экземпляров — они видят общее состояние
- Атомарные операции (sorted set + EXPIRE)
"""

from __future__ import annotations

import logging
import time
from typing import Tuple

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# ==============================================================
# Rate limiting (sliding window)
# ==============================================================

class RateLimiter:
    """
    Sliding window rate limiter через Redis sorted set.

    Принцип:
    - Ключ = "ratelimit:{scope}:{identifier}", например "ratelimit:webhook:1.2.3.4"
    - В sorted set храним timestamp каждого запроса как score
    - Перед каждым запросом удаляем всё старше окна
    - Считаем оставшееся — если >= limit, отказ
    """

    def __init__(self, redis: aioredis.Redis, limit: int, window_sec: int):
        """
        Args:
            redis: подключённый клиент Redis
            limit: максимум запросов в окне
            window_sec: размер окна в секундах
        """
        self.redis = redis
        self.limit = limit
        self.window_sec = window_sec

    async def check(self, identifier: str, scope: str = "webhook") -> Tuple[bool, int]:
        """
        Проверяет и регистрирует запрос.

        Returns:
            (allowed, remaining)
            - allowed: True если можно пропустить, False если превышен лимит
            - remaining: сколько ещё запросов доступно в текущем окне
        """
        now = time.time()
        key = f"ratelimit:{scope}:{identifier}"
        window_start = now - self.window_sec

        # Атомарный pipeline — все операции одним RTT к Redis
        async with self.redis.pipeline(transaction=True) as pipe:
            # 1. Удаляем старые записи (вне окна)
            pipe.zremrangebyscore(key, 0, window_start)
            # 2. Считаем сколько осталось
            pipe.zcard(key)
            # 3. Добавляем текущий запрос (timestamp как член и как score)
            pipe.zadd(key, {f"{now}": now})
            # 4. Ставим TTL чтобы Redis сам почистил неактивные ключи
            pipe.expire(key, self.window_sec + 1)
            results = await pipe.execute()

        # results[1] = количество ДО добавления нового
        current_count = int(results[1])
        remaining = max(0, self.limit - current_count - 1)
        allowed = current_count < self.limit

        return allowed, remaining


# ==============================================================
# Дедупликация
# ==============================================================

class Deduplicator:
    """
    Дедупликация через Redis SET NX (set if not exists).

    Принцип:
    - При первом приходе сигнала ставим ключ с TTL
    - Если ключ уже есть → дубликат
    - Атомарность гарантируется командой SET NX EX

    Ключ генерируется в schemas.py:WebhookRequest.dedup_key()
    Включает: symbol + side + timeframe + strategy
    То есть BUY BTCUSDT 3m liquidity_sweep дважды за 10 секунд = дубль.
    """

    def __init__(self, redis: aioredis.Redis, ttl_sec: int):
        self.redis = redis
        self.ttl_sec = ttl_sec

    async def is_duplicate(self, key: str) -> bool:
        """
        Returns True если такой же сигнал был < ttl_sec назад.

        Использует SET NX EX:
        - NX: ставим только если ключа нет
        - EX: с TTL
        - Возвращает True (поставили) или None (уже был)
        """
        result = await self.redis.set(key, "1", ex=self.ttl_sec, nx=True)
        # result == True если поставили (= это первый раз)
        # result is None если уже был (= это дубликат)
        return result is None
