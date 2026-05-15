"""
Real-Time Market Cache — Stage 6.

Хранилище рыночных данных в Redis.

Структура ключей:
- market:orderbook:{symbol}         → STRING (JSON) — последний снимок
- market:ticker:{symbol}            → STRING (JSON) — последний снимок
- market:trades:{symbol}            → LIST — последние N сделок (LPUSH + LTRIM)
- market:klines:{symbol}:{interval} → LIST — последние N свечей
- market:liquidations:{symbol}      → LIST — последние N ликвидаций

Дизайн:
- Async через redis.asyncio (используем уже существующий get_redis())
- Сериализация через orjson (быстрее json, уже в requirements)
- TTL на каждом ключе — данные сами устаревают
- max_history через LTRIM — не растёт бесконечно

Singleton + lifecycle:
- init_market_cache() / close_market_cache() / get_market_cache()
- Паттерн как у app/utils/redis_client.py

Использование:
    from app.cache.market_cache import get_market_cache
    cache = get_market_cache()
    await cache.update_ticker("BTCUSDT", {...})
    trades = await cache.get_trades("BTCUSDT", limit=50)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import orjson
import redis.asyncio as aioredis

from app.config import get_settings
from app.utils.redis_client import get_redis

logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class MarketCacheError(Exception):
    """Базовая ошибка кеша рыночных данных."""
    pass


class MarketCacheNotInitialized(MarketCacheError):
    """Singleton не инициализирован через init_market_cache()."""
    pass


# ============================================================
# Главный класс
# ============================================================

class MarketCache:
    """
    Async wrapper над Redis для рыночных данных.

    Все методы async (Redis client тоже async).
    Никакого in-memory кеша — Redis единственный источник истины.
    """

    # Префикс всех ключей — отделяет от других данных в Redis
    KEY_PREFIX = "market"

    def __init__(
        self,
        redis: aioredis.Redis,
        *,
        orderbook_ttl_sec: int = 60,
        ticker_ttl_sec: int = 30,
        trades_ttl_sec: int = 3600,
        klines_ttl_sec: int = 86400,
        liquidations_ttl_sec: int = 3600,
        max_trades: int = 500,
        max_klines: int = 200,
        max_liquidations: int = 100,
    ) -> None:
        self._redis = redis
        self._orderbook_ttl = orderbook_ttl_sec
        self._ticker_ttl = ticker_ttl_sec
        self._trades_ttl = trades_ttl_sec
        self._klines_ttl = klines_ttl_sec
        self._liquidations_ttl = liquidations_ttl_sec
        self._max_trades = max_trades
        self._max_klines = max_klines
        self._max_liquidations = max_liquidations

        logger.info(
            "MarketCache initialized: orderbook_ttl=%ds ticker_ttl=%ds "
            "trades_ttl=%ds klines_ttl=%ds liquidations_ttl=%ds "
            "max_trades=%d max_klines=%d max_liquidations=%d",
            orderbook_ttl_sec, ticker_ttl_sec, trades_ttl_sec,
            klines_ttl_sec, liquidations_ttl_sec,
            max_trades, max_klines, max_liquidations,
        )

    # --------------------------------------------------------
    # Factory из Settings
    # --------------------------------------------------------

    @classmethod
    def from_settings(cls, redis: aioredis.Redis, settings=None) -> "MarketCache":
        """Создать MarketCache из Pydantic Settings."""
        s = settings or get_settings()
        return cls(
            redis=redis,
            orderbook_ttl_sec=s.market_cache_orderbook_ttl_sec,
            ticker_ttl_sec=s.market_cache_ticker_ttl_sec,
            trades_ttl_sec=s.market_cache_trades_ttl_sec,
            klines_ttl_sec=s.market_cache_klines_ttl_sec,
            liquidations_ttl_sec=s.market_cache_liquidations_ttl_sec,
            max_trades=s.market_cache_max_trades,
            max_klines=s.market_cache_max_klines,
            max_liquidations=s.market_cache_max_liquidations,
        )

    # --------------------------------------------------------
    # Внутренние утилиты
    # --------------------------------------------------------

    def _key_orderbook(self, symbol: str) -> str:
        return f"{self.KEY_PREFIX}:orderbook:{symbol.upper()}"

    def _key_ticker(self, symbol: str) -> str:
        return f"{self.KEY_PREFIX}:ticker:{symbol.upper()}"

    def _key_trades(self, symbol: str) -> str:
        return f"{self.KEY_PREFIX}:trades:{symbol.upper()}"

    def _key_klines(self, symbol: str, interval: int) -> str:
        return f"{self.KEY_PREFIX}:klines:{symbol.upper()}:{interval}"

    def _key_liquidations(self, symbol: str) -> str:
        return f"{self.KEY_PREFIX}:liquidations:{symbol.upper()}"

    @staticmethod
    def _dumps(data: Any) -> bytes:
        """Сериализация через orjson (быстрее json)."""
        return orjson.dumps(data)

    @staticmethod
    def _loads(data: Optional[Any]) -> Any:
        """Десериализация. None → None."""
        if data is None:
            return None
        if isinstance(data, bytes):
            return orjson.loads(data)
        # decode_responses=True в redis_client делает str
        if isinstance(data, str):
            return orjson.loads(data)
        return data

    # --------------------------------------------------------
    # ORDERBOOK — последний снимок (SET с TTL)
    # --------------------------------------------------------

    async def update_orderbook(self, symbol: str, data: dict) -> None:
        """Обновить последний снимок orderbook."""
        try:
            key = self._key_orderbook(symbol)
            await self._redis.set(key, self._dumps(data), ex=self._orderbook_ttl)
        except Exception:
            logger.exception("Failed to update orderbook for %s", symbol)

    async def get_orderbook(self, symbol: str) -> Optional[dict]:
        """Получить последний снимок orderbook. None если нет/устарел."""
        try:
            key = self._key_orderbook(symbol)
            raw = await self._redis.get(key)
            return self._loads(raw)
        except Exception:
            logger.exception("Failed to get orderbook for %s", symbol)
            return None

    # --------------------------------------------------------
    # TICKER — последний снимок (SET с TTL)
    # --------------------------------------------------------

    async def update_ticker(self, symbol: str, data: dict) -> None:
        """Обновить последний снимок ticker."""
        try:
            key = self._key_ticker(symbol)
            await self._redis.set(key, self._dumps(data), ex=self._ticker_ttl)
        except Exception:
            logger.exception("Failed to update ticker for %s", symbol)

    async def get_ticker(self, symbol: str) -> Optional[dict]:
        """Получить последний снимок ticker. None если нет/устарел."""
        try:
            key = self._key_ticker(symbol)
            raw = await self._redis.get(key)
            return self._loads(raw)
        except Exception:
            logger.exception("Failed to get ticker for %s", symbol)
            return None

    # --------------------------------------------------------
    # TRADES — лента (LPUSH + LTRIM + EXPIRE)
    # --------------------------------------------------------

    async def add_trade(self, symbol: str, trade: dict) -> None:
        """
        Добавить сделку в начало списка. Старые отбрасываются (LTRIM).
        Используем pipeline для атомарности.
        """
        try:
            key = self._key_trades(symbol)
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.lpush(key, self._dumps(trade))
                pipe.ltrim(key, 0, self._max_trades - 1)
                pipe.expire(key, self._trades_ttl)
                await pipe.execute()
        except Exception:
            logger.exception("Failed to add trade for %s", symbol)

    async def get_trades(self, symbol: str, limit: int = 50) -> list[dict]:
        """
        Получить последние N сделок (новейшие первыми).
        Возвращает [] если данных нет.
        """
        try:
            key = self._key_trades(symbol)
            raw_list = await self._redis.lrange(key, 0, limit - 1)
            return [self._loads(r) for r in raw_list if r is not None]
        except Exception:
            logger.exception("Failed to get trades for %s", symbol)
            return []

    # --------------------------------------------------------
    # KLINES — лента на каждый интервал (LPUSH + LTRIM + EXPIRE)
    # --------------------------------------------------------

    async def add_kline(self, symbol: str, interval: int, kline: dict) -> None:
        """Добавить свечу в начало списка. Старые отбрасываются."""
        try:
            key = self._key_klines(symbol, interval)
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.lpush(key, self._dumps(kline))
                pipe.ltrim(key, 0, self._max_klines - 1)
                pipe.expire(key, self._klines_ttl)
                await pipe.execute()
        except Exception:
            logger.exception(
                "Failed to add kline for %s interval=%d", symbol, interval,
            )

    async def get_klines(
        self, symbol: str, interval: int, limit: int = 100,
    ) -> list[dict]:
        """Получить последние N свечей (новейшие первыми)."""
        try:
            key = self._key_klines(symbol, interval)
            raw_list = await self._redis.lrange(key, 0, limit - 1)
            return [self._loads(r) for r in raw_list if r is not None]
        except Exception:
            logger.exception(
                "Failed to get klines for %s interval=%d", symbol, interval,
            )
            return []

    # --------------------------------------------------------
    # LIQUIDATIONS — лента (LPUSH + LTRIM + EXPIRE)
    # --------------------------------------------------------

    async def add_liquidation(self, symbol: str, liquidation: dict) -> None:
        """Добавить ликвидацию в начало списка."""
        try:
            key = self._key_liquidations(symbol)
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.lpush(key, self._dumps(liquidation))
                pipe.ltrim(key, 0, self._max_liquidations - 1)
                pipe.expire(key, self._liquidations_ttl)
                await pipe.execute()
        except Exception:
            logger.exception("Failed to add liquidation for %s", symbol)

    async def get_liquidations(self, symbol: str, limit: int = 50) -> list[dict]:
        """Получить последние N ликвидаций (новейшие первыми)."""
        try:
            key = self._key_liquidations(symbol)
            raw_list = await self._redis.lrange(key, 0, limit - 1)
            return [self._loads(r) for r in raw_list if r is not None]
        except Exception:
            logger.exception("Failed to get liquidations for %s", symbol)
            return []

    # --------------------------------------------------------
    # Утилиты
    # --------------------------------------------------------

    async def is_healthy(self) -> bool:
        """
        True если Redis отвечает на ping.
        Не проверяем «есть ли данные» — это задача health endpoint.
        """
        try:
            return bool(await self._redis.ping())
        except Exception:
            logger.exception("MarketCache health check failed")
            return False

    async def clear_symbol(self, symbol: str) -> int:
        """
        Удалить все ключи для символа. Используется в тестах.
        Returns: число удалённых ключей.
        """
        symbol = symbol.upper()
        keys_to_delete = [
            self._key_orderbook(symbol),
            self._key_ticker(symbol),
            self._key_trades(symbol),
            self._key_liquidations(symbol),
        ]
        # Добавляем все возможные kline-интервалы (запросим из Redis)
        try:
            pattern = f"{self.KEY_PREFIX}:klines:{symbol}:*"
            async for k in self._redis.scan_iter(match=pattern, count=100):
                keys_to_delete.append(k)
            if not keys_to_delete:
                return 0
            return await self._redis.delete(*keys_to_delete)
        except Exception:
            logger.exception("Failed to clear symbol %s", symbol)
            return 0

    async def get_stats(self) -> dict:
        """
        Статистика кеша — сколько ключей по каждому типу.
        Для health endpoint и debug.
        """
        try:
            stats: dict[str, Any] = {}
            for stream in ("orderbook", "ticker", "trades", "klines", "liquidations"):
                pattern = f"{self.KEY_PREFIX}:{stream}:*"
                count = 0
                async for _ in self._redis.scan_iter(match=pattern, count=100):
                    count += 1
                stats[stream] = count
            return stats
        except Exception:
            logger.exception("Failed to get cache stats")
            return {}


# ============================================================
# Singleton + lifecycle (паттерн как redis_client / websocket_client)
# ============================================================

_market_cache: Optional[MarketCache] = None


async def init_market_cache() -> Optional[MarketCache]:
    """
    Создаёт MarketCache используя уже инициализированный Redis.

    Вызывается из main.py:lifespan() после init_redis() и init_websocket().
    Если market_cache_enabled = False → возвращает None.
    """
    global _market_cache

    settings = get_settings()
    if not settings.market_cache_enabled:
        logger.info("MarketCache disabled (market_cache_enabled=False), skipping init")
        return None

    redis = get_redis()  # должен быть уже инициализирован
    cache = MarketCache.from_settings(redis, settings)
    _market_cache = cache
    logger.info("MarketCache singleton initialized")
    return cache


async def close_market_cache() -> None:
    """Обнуляет singleton. Redis закрывается через close_redis() отдельно."""
    global _market_cache
    if _market_cache is not None:
        _market_cache = None
        logger.info("MarketCache singleton closed")


def get_market_cache() -> MarketCache:
    """
    Получить инициализированный singleton.
    Raises MarketCacheNotInitialized если init не выполнялся.
    """
    if _market_cache is None:
        raise MarketCacheNotInitialized(
            "MarketCache not initialized. "
            "Check lifespan() in main.py and market_cache_enabled in settings."
        )
    return _market_cache