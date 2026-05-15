"""
Юнит-тесты для app/cache/market_cache.py (Stage 6).

Всё мокается:
- aioredis.Redis → AsyncMock с заглушками всех методов
- get_redis() → возвращает наш мок

Никаких реальных подключений к Redis.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest

from app.cache import market_cache as mc_module
from app.cache.market_cache import (
    MarketCache,
    MarketCacheError,
    MarketCacheNotInitialized,
    close_market_cache,
    get_market_cache,
    init_market_cache,
)


# ============================================================
# Фикстуры
# ============================================================

@pytest.fixture
def mock_redis():
    """Mock async Redis клиента — все методы возвращают разумные дефолты."""
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.lrange = AsyncMock(return_value=[])
    redis.delete = AsyncMock(return_value=0)
    redis.ping = AsyncMock(return_value=True)

    # Pipeline возвращает context manager с теми же методами
    pipe = AsyncMock()
    pipe.lpush = MagicMock(return_value=pipe)
    pipe.ltrim = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[1, True, True])
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=None)
    redis.pipeline = MagicMock(return_value=pipe)

    # scan_iter — async generator (yield ничего)
    async def empty_scan_iter(*args, **kwargs):
        if False:
            yield
    redis.scan_iter = empty_scan_iter

    return redis


@pytest.fixture
def cache(mock_redis):
    """Готовый MarketCache на мокнутом Redis."""
    return MarketCache(
        redis=mock_redis,
        orderbook_ttl_sec=60,
        ticker_ttl_sec=30,
        trades_ttl_sec=3600,
        klines_ttl_sec=86400,
        liquidations_ttl_sec=3600,
        max_trades=500,
        max_klines=200,
        max_liquidations=100,
    )


@pytest.fixture(autouse=True)
def reset_singleton():
    """Сбрасываем модульный singleton до и после каждого теста."""
    mc_module._market_cache = None
    yield
    mc_module._market_cache = None


# ============================================================
# Группа 1 — Конструктор и factory
# ============================================================

@pytest.mark.unit
class TestConstructor:
    def test_init_with_defaults(self, mock_redis):
        c = MarketCache(redis=mock_redis)
        assert c._redis is mock_redis
        assert c._orderbook_ttl == 60
        assert c._ticker_ttl == 30
        assert c._trades_ttl == 3600
        assert c._klines_ttl == 86400
        assert c._liquidations_ttl == 3600
        assert c._max_trades == 500
        assert c._max_klines == 200
        assert c._max_liquidations == 100

    def test_init_custom_params(self, mock_redis):
        c = MarketCache(
            redis=mock_redis,
            orderbook_ttl_sec=120,
            max_trades=1000,
        )
        assert c._orderbook_ttl == 120
        assert c._max_trades == 1000

    def test_from_settings(self, mock_redis):
        fake_settings = MagicMock()
        fake_settings.market_cache_orderbook_ttl_sec = 120
        fake_settings.market_cache_ticker_ttl_sec = 45
        fake_settings.market_cache_trades_ttl_sec = 7200
        fake_settings.market_cache_klines_ttl_sec = 100000
        fake_settings.market_cache_liquidations_ttl_sec = 7200
        fake_settings.market_cache_max_trades = 1000
        fake_settings.market_cache_max_klines = 300
        fake_settings.market_cache_max_liquidations = 200

        c = MarketCache.from_settings(mock_redis, fake_settings)
        assert c._orderbook_ttl == 120
        assert c._ticker_ttl == 45
        assert c._max_trades == 1000


# ============================================================
# Группа 2 — Ключи
# ============================================================

@pytest.mark.unit
class TestKeys:
    def test_key_orderbook_uppercase(self, cache):
        assert cache._key_orderbook("btcusdt") == "market:orderbook:BTCUSDT"

    def test_key_ticker(self, cache):
        assert cache._key_ticker("BTCUSDT") == "market:ticker:BTCUSDT"

    def test_key_trades(self, cache):
        assert cache._key_trades("ethusdt") == "market:trades:ETHUSDT"

    def test_key_klines_with_interval(self, cache):
        assert cache._key_klines("BTCUSDT", 15) == "market:klines:BTCUSDT:15"

    def test_key_liquidations(self, cache):
        assert cache._key_liquidations("btcusdt") == "market:liquidations:BTCUSDT"

    def test_key_prefix_constant(self):
        assert MarketCache.KEY_PREFIX == "market"


# ============================================================
# Группа 3 — Сериализация
# ============================================================

@pytest.mark.unit
class TestSerialization:
    def test_dumps_returns_bytes(self):
        result = MarketCache._dumps({"a": 1})
        assert isinstance(result, bytes)

    def test_dumps_loads_roundtrip(self):
        data = {"price": "60000.5", "volume": "1.5", "ts": 12345}
        dumped = MarketCache._dumps(data)
        loaded = MarketCache._loads(dumped)
        assert loaded == data

    def test_loads_none(self):
        assert MarketCache._loads(None) is None

    def test_loads_string(self):
        json_str = '{"a": 1}'
        assert MarketCache._loads(json_str) == {"a": 1}

    def test_loads_bytes(self):
        json_bytes = orjson.dumps({"a": 1})
        assert MarketCache._loads(json_bytes) == {"a": 1}


# ============================================================
# Группа 4 — ORDERBOOK
# ============================================================

@pytest.mark.unit
class TestOrderbook:
    @pytest.mark.asyncio
    async def test_update_orderbook_calls_set_with_ttl(self, cache, mock_redis):
        data = {"b": [["60000", "1.5"]], "a": [["60001", "0.5"]], "ts": 12345}
        await cache.update_orderbook("BTCUSDT", data)
        mock_redis.set.assert_awaited_once()
        call_args = mock_redis.set.call_args
        assert call_args.args[0] == "market:orderbook:BTCUSDT"
        # ttl передаётся через ex=
        assert call_args.kwargs["ex"] == 60

    @pytest.mark.asyncio
    async def test_update_orderbook_uppercases_symbol(self, cache, mock_redis):
        await cache.update_orderbook("btcusdt", {"b": [], "a": []})
        call_args = mock_redis.set.call_args
        assert "BTCUSDT" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_dict(self, cache, mock_redis):
        data = {"b": [["60000", "1.5"]], "a": [["60001", "0.5"]], "ts": 12345}
        mock_redis.get.return_value = orjson.dumps(data)
        result = await cache.get_orderbook("BTCUSDT")
        assert result == data

    @pytest.mark.asyncio
    async def test_get_orderbook_returns_none_if_missing(self, cache, mock_redis):
        mock_redis.get.return_value = None
        assert await cache.get_orderbook("BTCUSDT") is None

    @pytest.mark.asyncio
    async def test_update_orderbook_swallows_exception(self, cache, mock_redis):
        # Если Redis падает — не пробрасываем исключение, а логируем
        mock_redis.set.side_effect = Exception("Redis down")
        # Не должно бросить — внутри try/except
        await cache.update_orderbook("BTCUSDT", {"b": [], "a": []})


# ============================================================
# Группа 5 — TICKER
# ============================================================

@pytest.mark.unit
class TestTicker:
    @pytest.mark.asyncio
    async def test_update_ticker(self, cache, mock_redis):
        data = {"lastPrice": "60000", "markPrice": "60000.5"}
        await cache.update_ticker("BTCUSDT", data)
        mock_redis.set.assert_awaited_once()
        assert mock_redis.set.call_args.kwargs["ex"] == 30

    @pytest.mark.asyncio
    async def test_get_ticker(self, cache, mock_redis):
        data = {"lastPrice": "60000"}
        mock_redis.get.return_value = orjson.dumps(data)
        assert await cache.get_ticker("BTCUSDT") == data

    @pytest.mark.asyncio
    async def test_get_ticker_none(self, cache, mock_redis):
        mock_redis.get.return_value = None
        assert await cache.get_ticker("BTCUSDT") is None


# ============================================================
# Группа 6 — TRADES (LIST + LTRIM)
# ============================================================

@pytest.mark.unit
class TestTrades:
    @pytest.mark.asyncio
    async def test_add_trade_uses_pipeline(self, cache, mock_redis):
        trade = {"price": "60000", "qty": "0.1", "side": "Buy"}
        await cache.add_trade("BTCUSDT", trade)
        # pipeline создаётся
        mock_redis.pipeline.assert_called_once()
        # LPUSH, LTRIM, EXPIRE вызываются на pipeline
        pipe = mock_redis.pipeline.return_value
        pipe.lpush.assert_called_once()
        pipe.ltrim.assert_called_once_with("market:trades:BTCUSDT", 0, 499)
        pipe.expire.assert_called_once_with("market:trades:BTCUSDT", 3600)
        pipe.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_trades_returns_list(self, cache, mock_redis):
        trades_data = [
            {"price": "60001", "ts": 2},
            {"price": "60000", "ts": 1},
        ]
        mock_redis.lrange.return_value = [orjson.dumps(t) for t in trades_data]
        result = await cache.get_trades("BTCUSDT", limit=10)
        assert result == trades_data
        # lrange вызван с правильными параметрами
        mock_redis.lrange.assert_awaited_once_with("market:trades:BTCUSDT", 0, 9)

    @pytest.mark.asyncio
    async def test_get_trades_returns_empty_list_if_no_data(self, cache, mock_redis):
        mock_redis.lrange.return_value = []
        assert await cache.get_trades("BTCUSDT") == []

    @pytest.mark.asyncio
    async def test_add_trade_swallows_exception(self, cache, mock_redis):
        mock_redis.pipeline.side_effect = Exception("Redis down")
        await cache.add_trade("BTCUSDT", {"price": "0"})


# ============================================================
# Группа 7 — KLINES (LIST + LTRIM, per interval)
# ============================================================

@pytest.mark.unit
class TestKlines:
    @pytest.mark.asyncio
    async def test_add_kline_uses_correct_key(self, cache, mock_redis):
        kline = {"start": 1, "close": "60000"}
        await cache.add_kline("BTCUSDT", 15, kline)
        pipe = mock_redis.pipeline.return_value
        pipe.lpush.assert_called_once()
        # Ключ содержит interval
        call_args = pipe.lpush.call_args.args
        assert call_args[0] == "market:klines:BTCUSDT:15"
        pipe.ltrim.assert_called_once_with("market:klines:BTCUSDT:15", 0, 199)
        pipe.expire.assert_called_once_with("market:klines:BTCUSDT:15", 86400)

    @pytest.mark.asyncio
    async def test_get_klines(self, cache, mock_redis):
        klines = [{"start": 2, "close": "61000"}, {"start": 1, "close": "60000"}]
        mock_redis.lrange.return_value = [orjson.dumps(k) for k in klines]
        result = await cache.get_klines("BTCUSDT", interval=1, limit=10)
        assert result == klines
        mock_redis.lrange.assert_awaited_once_with("market:klines:BTCUSDT:1", 0, 9)

    @pytest.mark.asyncio
    async def test_get_klines_empty(self, cache, mock_redis):
        mock_redis.lrange.return_value = []
        assert await cache.get_klines("BTCUSDT", interval=1) == []


# ============================================================
# Группа 8 — LIQUIDATIONS
# ============================================================

@pytest.mark.unit
class TestLiquidations:
    @pytest.mark.asyncio
    async def test_add_liquidation(self, cache, mock_redis):
        liq = {"side": "Sell", "price": "60000", "qty": "1.0"}
        await cache.add_liquidation("BTCUSDT", liq)
        pipe = mock_redis.pipeline.return_value
        pipe.ltrim.assert_called_once_with("market:liquidations:BTCUSDT", 0, 99)
        pipe.expire.assert_called_once_with("market:liquidations:BTCUSDT", 3600)

    @pytest.mark.asyncio
    async def test_get_liquidations(self, cache, mock_redis):
        liqs = [{"side": "Sell", "price": "60000"}]
        mock_redis.lrange.return_value = [orjson.dumps(l) for l in liqs]
        result = await cache.get_liquidations("BTCUSDT", limit=10)
        assert result == liqs


# ============================================================
# Группа 9 — Утилиты (is_healthy, clear_symbol, get_stats)
# ============================================================

@pytest.mark.unit
class TestUtils:
    @pytest.mark.asyncio
    async def test_is_healthy_true(self, cache, mock_redis):
        mock_redis.ping.return_value = True
        assert await cache.is_healthy() is True

    @pytest.mark.asyncio
    async def test_is_healthy_false_on_exception(self, cache, mock_redis):
        mock_redis.ping.side_effect = Exception("connection refused")
        assert await cache.is_healthy() is False

    @pytest.mark.asyncio
    async def test_clear_symbol_no_klines(self, cache, mock_redis):
        # scan_iter возвращает пусто, delete вызывается с базовыми ключами
        mock_redis.delete.return_value = 4
        result = await cache.clear_symbol("BTCUSDT")
        assert result == 4
        mock_redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_stats_returns_dict(self, cache, mock_redis):
        # scan_iter пустой → каждое значение по 0
        stats = await cache.get_stats()
        assert isinstance(stats, dict)
        assert set(stats.keys()) == {
            "orderbook", "ticker", "trades", "klines", "liquidations"
        }


# ============================================================
# Группа 10 — Singleton lifecycle
# ============================================================

@pytest.mark.unit
class TestSingletonLifecycle:
    def test_get_market_cache_raises_if_not_initialized(self):
        with pytest.raises(MarketCacheNotInitialized):
            get_market_cache()

    @pytest.mark.asyncio
    async def test_init_market_cache_disabled_returns_none(self):
        with patch.object(mc_module, "get_settings") as mock_get:
            fake_settings = MagicMock()
            fake_settings.market_cache_enabled = False
            mock_get.return_value = fake_settings

            result = await init_market_cache()
            assert result is None
            assert mc_module._market_cache is None

    @pytest.mark.asyncio
    async def test_init_market_cache_creates_singleton(self, mock_redis):
        with patch.object(mc_module, "get_settings") as mock_get, \
             patch.object(mc_module, "get_redis", return_value=mock_redis):
            fake_settings = MagicMock()
            fake_settings.market_cache_enabled = True
            fake_settings.market_cache_orderbook_ttl_sec = 60
            fake_settings.market_cache_ticker_ttl_sec = 30
            fake_settings.market_cache_trades_ttl_sec = 3600
            fake_settings.market_cache_klines_ttl_sec = 86400
            fake_settings.market_cache_liquidations_ttl_sec = 3600
            fake_settings.market_cache_max_trades = 500
            fake_settings.market_cache_max_klines = 200
            fake_settings.market_cache_max_liquidations = 100
            mock_get.return_value = fake_settings

            result = await init_market_cache()
            assert result is not None
            assert mc_module._market_cache is result

    @pytest.mark.asyncio
    async def test_close_market_cache_resets_singleton(self, mock_redis):
        # Сначала проинициализируем
        with patch.object(mc_module, "get_settings") as mock_get, \
             patch.object(mc_module, "get_redis", return_value=mock_redis):
            fake_settings = MagicMock()
            fake_settings.market_cache_enabled = True
            fake_settings.market_cache_orderbook_ttl_sec = 60
            fake_settings.market_cache_ticker_ttl_sec = 30
            fake_settings.market_cache_trades_ttl_sec = 3600
            fake_settings.market_cache_klines_ttl_sec = 86400
            fake_settings.market_cache_liquidations_ttl_sec = 3600
            fake_settings.market_cache_max_trades = 500
            fake_settings.market_cache_max_klines = 200
            fake_settings.market_cache_max_liquidations = 100
            mock_get.return_value = fake_settings

            await init_market_cache()
            assert mc_module._market_cache is not None

            await close_market_cache()
            assert mc_module._market_cache is None

    @pytest.mark.asyncio
    async def test_close_market_cache_no_op_if_not_initialized(self):
        # Не должен падать если singleton уже None
        await close_market_cache()
        assert mc_module._market_cache is None


# ============================================================
# Группа 11 — Исключения
# ============================================================

@pytest.mark.unit
class TestExceptions:
    def test_market_cache_not_initialized_is_subclass(self):
        assert issubclass(MarketCacheNotInitialized, MarketCacheError)

    def test_market_cache_error_is_exception(self):
        assert issubclass(MarketCacheError, Exception)