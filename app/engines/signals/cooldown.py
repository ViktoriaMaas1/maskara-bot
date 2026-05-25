"""CooldownGate — антиспам через Redis с TTL.

Не даёт SignalGenerator отправлять один и тот же сигнал чаще, чем раз в N секунд.
Ключи в Redis: signal_cooldown:{symbol}:{action}, TTL = N секунд.

Использование:
    gate = CooldownGate(redis_client, ttl_seconds=60)
    if await gate.is_allowed("BTCUSDT", "BUY"):
        ... отправить сигнал ...
        await gate.mark_sent("BTCUSDT", "BUY")
"""

from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


# Префикс ключей в Redis (изолирует от других сервисов)
COOLDOWN_KEY_PREFIX = "signal_cooldown"

# Дефолтный TTL — 60 секунд (как в STAGE_8_PLAN: SIGNAL_COOLDOWN_SEC=60)
DEFAULT_COOLDOWN_TTL_SEC = 60


class CooldownGate:
    """Антиспам-механизм для сигналов через Redis TTL.

    Ставит ключ signal_cooldown:{symbol}:{action} с TTL.
    Пока ключ существует — повторный сигнал блокируется.
    Redis сам удаляет ключ по истечении TTL — ручная очистка не нужна.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        ttl_seconds: int = DEFAULT_COOLDOWN_TTL_SEC,
        key_prefix: str = COOLDOWN_KEY_PREFIX,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, got {ttl_seconds}")

        self._redis = redis_client
        self._ttl = ttl_seconds
        self._prefix = key_prefix

    # ============================================================
    # Внутреннее
    # ============================================================

    def _build_key(self, symbol: str, action: str) -> str:
        """Сформировать ключ Redis: signal_cooldown:BTCUSDT:BUY"""
        return f"{self._prefix}:{symbol}:{action}"

    # ============================================================
    # Публичный API
    # ============================================================

    async def is_allowed(self, symbol: str, action: str) -> bool:
        """Можно ли отправить сигнал (symbol, action) прямо сейчас?

        True  — ключа в Redis нет, cooldown свободен.
        False — ключ есть, ждём истечения TTL.
        """
        key = self._build_key(symbol, action)
        exists = await self._redis.exists(key)
        allowed = exists == 0
        if not allowed:
            logger.debug(
                "Cooldown активен",
                extra={"symbol": symbol, "action": action, "key": key},
            )
        return allowed

    async def mark_sent(self, symbol: str, action: str) -> None:
        """Зафиксировать факт отправки сигнала — поставить ключ с TTL.

        Следующие N секунд is_allowed() будет возвращать False.
        SET с EX (TTL) — атомарная операция, race-condition'ы не страшны.
        """
        key = self._build_key(symbol, action)
        await self._redis.set(key, "1", ex=self._ttl)
        logger.info(
            "Cooldown поставлен",
            extra={
                "symbol": symbol,
                "action": action,
                "ttl_seconds": self._ttl,
                "key": key,
            },
        )

    async def ttl_remaining(self, symbol: str, action: str) -> int:
        """Сколько секунд до разблокировки.

        Возвращает:
            > 0 — секунды до конца cooldown
              0 — cooldown свободен (ключа нет)
             -1 — ключ есть, но без TTL (не должно случиться в нашей логике)
        """
        key = self._build_key(symbol, action)
        ttl = await self._redis.ttl(key)
        # redis-py: ttl == -2 если ключа нет, -1 если без TTL, >= 0 если живёт
        if ttl == -2:
            return 0
        return int(ttl)

    async def clear(self, symbol: str, action: str) -> None:
        """Принудительно снять cooldown (для отладки/тестов).

        Удаляет ключ из Redis. После этого is_allowed() вернёт True.
        """
        key = self._build_key(symbol, action)
        await self._redis.delete(key)
        logger.info(
            "Cooldown сброшен",
            extra={"symbol": symbol, "action": action, "key": key},
        )