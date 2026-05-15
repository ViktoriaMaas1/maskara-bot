"""
Юнит-тесты для app/bybit/websocket_client.py (Stage 5).

Всё мокается:
- pybit.unified_trading.WebSocket → MagicMock
- asyncio.sleep → AsyncMock (чтобы тесты не ждали)
- time.time → patched в нужных местах

Никаких реальных сетевых подключений.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.bybit import websocket_client as ws_module
from app.bybit.websocket_client import (
    BybitWebSocketClient,
    BybitWebSocketError,
    WebSocketNotInitialized,
    close_websocket,
    get_websocket,
    init_websocket,
)


# ============================================================
# Фикстуры
# ============================================================

@pytest.fixture
def client():
    """Создаёт BybitWebSocketClient с дефолтными параметрами для тестов."""
    return BybitWebSocketClient(
        symbols=["BTCUSDT", "ETHUSDT"],
        testnet=True,
        orderbook_depth=50,
        kline_intervals=[1, 15],
        reconnect_base_delay_sec=0.01,  # ускоряем для тестов
        reconnect_max_delay_sec=0.05,
        ping_interval_sec=20,
    )


@pytest.fixture(autouse=True)
def reset_singleton():
    """Сбрасываем модульный singleton до и после каждого теста."""
    ws_module._ws_client = None
    yield
    ws_module._ws_client = None


# ============================================================
# Группа 1 — Конструктор и factory
# ============================================================

@pytest.mark.unit
class TestConstructor:
    def test_init_with_symbols(self):
        c = BybitWebSocketClient(symbols=["BTCUSDT"])
        assert c._symbols == ["BTCUSDT"]
        assert c._testnet is True
        assert c._orderbook_depth == 50
        assert c._kline_intervals == [1, 3, 15, 60]  # дефолт

    def test_init_raises_on_empty_symbols(self):
        with pytest.raises(ValueError, match="symbol"):
            BybitWebSocketClient(symbols=[])

    def test_symbols_uppercased(self):
        c = BybitWebSocketClient(symbols=["btcusdt", "EthUsdt"])
        assert c._symbols == ["BTCUSDT", "ETHUSDT"]

    def test_custom_kline_intervals(self):
        c = BybitWebSocketClient(symbols=["BTCUSDT"], kline_intervals=[1, 5])
        assert c._kline_intervals == [1, 5]

    def test_from_settings_parses_intervals(self):
        fake_settings = MagicMock()
        fake_settings.allowed_symbols_list = ["BTCUSDT", "ETHUSDT"]
        fake_settings.bybit_testnet = True
        fake_settings.bybit_ws_orderbook_depth = 200
        fake_settings.bybit_ws_kline_intervals = "1, 5, 15, 60"
        fake_settings.bybit_ws_reconnect_base_delay_sec = 1.0
        fake_settings.bybit_ws_reconnect_max_delay_sec = 60.0
        fake_settings.bybit_ws_ping_interval_sec = 20

        c = BybitWebSocketClient.from_settings(fake_settings)
        assert c._symbols == ["BTCUSDT", "ETHUSDT"]
        assert c._kline_intervals == [1, 5, 15, 60]
        assert c._orderbook_depth == 200

    def test_initial_state(self, client):
        assert client._connected is False
        assert client._last_message_ts == 0.0
        assert client._failed_attempts == 0
        assert client._ws is None
        assert client._task is None


# ============================================================
# Группа 2 — Callbacks (парсинг payload)
# ============================================================

@pytest.mark.unit
class TestCallbacks:
    def test_on_orderbook_stores_snapshot(self, client):
        msg = {
            "topic": "orderbook.50.BTCUSDT",
            "ts": 1715541234567,
            "data": {
                "s": "BTCUSDT",
                "b": [["60000.0", "1.5"], ["59999.0", "2.0"]],
                "a": [["60001.0", "0.5"]],
                "u": 123,
                "seq": 456,
            },
        }
        client._on_orderbook(msg)

        snap = client.get_latest("orderbook", "BTCUSDT")
        assert snap is not None
        assert snap["b"][0] == ["60000.0", "1.5"]
        assert snap["a"][0] == ["60001.0", "0.5"]
        assert snap["ts"] == 1715541234567
        assert client._last_message_ts > 0

    def test_on_orderbook_missing_symbol_ignored(self, client):
        msg = {"topic": "orderbook.50.", "ts": 1, "data": {}}
        client._on_orderbook(msg)
        assert client.get_latest("orderbook", "BTCUSDT") is None

    def test_on_trade_appends_to_deque(self, client):
        msg = {
            "topic": "publicTrade.BTCUSDT",
            "data": [
                {"s": "BTCUSDT", "T": 100, "S": "Buy", "p": "60000", "v": "0.1", "i": "t1"},
                {"s": "BTCUSDT", "T": 101, "S": "Sell", "p": "60001", "v": "0.2", "i": "t2"},
            ],
        }
        client._on_trade(msg)
        trades = client.get_latest("trade", "BTCUSDT")
        assert len(trades) == 2
        assert trades[0]["side"] == "Buy"
        assert trades[1]["price"] == "60001"

    def test_on_trade_deque_maxlen(self, client):
        # Шлём 150 сделок — деке должен оставить только последние 100
        for i in range(150):
            client._on_trade({
                "data": [{"s": "BTCUSDT", "T": i, "S": "Buy", "p": "0", "v": "0", "i": f"t{i}"}],
            })
        trades = client.get_latest("trade", "BTCUSDT")
        assert len(trades) == 100
        # Первая сделка должна быть с T=50 (старые отброшены)
        assert trades[0]["ts"] == 50
        assert trades[-1]["ts"] == 149

    def test_on_ticker_stores_snapshot(self, client):
        msg = {
            "topic": "tickers.BTCUSDT",
            "ts": 1,
            "data": {
                "symbol": "BTCUSDT",
                "lastPrice": "60000",
                "markPrice": "59999",
                "indexPrice": "59998",
                "bid1Price": "59995",
                "ask1Price": "60005",
                "volume24h": "1000",
                "turnover24h": "60000000",
                "openInterest": "500",
                "fundingRate": "0.0001",
            },
        }
        client._on_ticker(msg)
        t = client.get_latest("ticker", "BTCUSDT")
        assert t["lastPrice"] == "60000"
        assert t["fundingRate"] == "0.0001"

    def test_on_kline_parses_topic(self, client):
        msg = {
            "topic": "kline.1.BTCUSDT",
            "data": [{
                "start": 1000,
                "end": 1060,
                "interval": "1",
                "open": "60000",
                "close": "60010",
                "high": "60020",
                "low": "59990",
                "volume": "10",
                "turnover": "600100",
                "confirm": False,
            }],
        }
        client._on_kline(msg)
        klines = client.get_latest("kline", "BTCUSDT", interval=1)
        assert len(klines) == 1
        assert klines[0]["close"] == "60010"

    def test_on_kline_separates_intervals(self, client):
        # 1m и 15m свечи для одного символа — должны храниться раздельно
        for interval, close in [("1", "60000"), ("15", "60500")]:
            client._on_kline({
                "topic": f"kline.{interval}.BTCUSDT",
                "data": [{
                    "start": 0, "end": 0, "interval": interval,
                    "open": "0", "close": close, "high": "0", "low": "0",
                    "volume": "0", "turnover": "0", "confirm": True,
                }],
            })
        all_tfs = client.get_latest("kline", "BTCUSDT")
        assert 1 in all_tfs
        assert 15 in all_tfs
        # один список для интервала 1
        assert client.get_latest("kline", "BTCUSDT", interval=1)[0]["close"] == "60000"
        assert client.get_latest("kline", "BTCUSDT", interval=15)[0]["close"] == "60500"

    def test_on_liquidation_handles_list_payload(self, client):
        msg = {
            "topic": "allLiquidation.BTCUSDT",
            "data": [
                {"symbol": "BTCUSDT", "side": "Sell", "price": "60000", "size": "1.0", "updatedTime": 1000},
            ],
        }
        client._on_liquidation(msg)
        liq = client.get_latest("liquidation", "BTCUSDT")
        assert len(liq) == 1
        assert liq[0]["side"] == "Sell"

    def test_on_liquidation_handles_dict_payload(self, client):
        msg = {
            "topic": "liquidation.BTCUSDT",
            "data": {"symbol": "BTCUSDT", "side": "Buy", "price": "60000", "size": "0.5", "updatedTime": 2000},
        }
        client._on_liquidation(msg)
        liq = client.get_latest("liquidation", "BTCUSDT")
        assert len(liq) == 1
        assert liq[0]["side"] == "Buy"

    def test_broken_payload_does_not_crash(self, client):
        # Намеренно битый payload (data = None)
        client._on_orderbook({"data": None})
        client._on_trade({"data": None})
        client._on_ticker({"data": None})
        client._on_kline({"data": None, "topic": "kline.X.X"})
        client._on_liquidation({"data": None})
        # Клиент должен жить — никаких исключений наружу
        assert client.get_latest("orderbook", "BTCUSDT") is None


# ============================================================
# Группа 3 — get_latest()
# ============================================================

@pytest.mark.unit
class TestGetLatest:
    def test_returns_none_for_missing_data(self, client):
        assert client.get_latest("orderbook", "BTCUSDT") is None
        assert client.get_latest("trade", "BTCUSDT") is None
        assert client.get_latest("ticker", "BTCUSDT") is None

    def test_returns_list_for_trade(self, client):
        client._on_trade({"data": [{"s": "BTCUSDT", "T": 1, "S": "Buy", "p": "0", "v": "0", "i": "t1"}]})
        result = client.get_latest("trade", "BTCUSDT")
        assert isinstance(result, list)  # deque преобразуется в list

    def test_returns_dict_for_orderbook(self, client):
        client._on_orderbook({"data": {"s": "BTCUSDT", "b": [], "a": []}, "ts": 1})
        result = client.get_latest("orderbook", "BTCUSDT")
        assert isinstance(result, dict)

    def test_unknown_stream_returns_none(self, client):
        assert client.get_latest("nonexistent", "BTCUSDT") is None

    def test_kline_without_interval_returns_all(self, client):
        client._on_kline({
            "topic": "kline.1.BTCUSDT",
            "data": [{"start": 0, "end": 0, "interval": "1", "open": "0", "close": "0",
                      "high": "0", "low": "0", "volume": "0", "turnover": "0", "confirm": True}],
        })
        result = client.get_latest("kline", "BTCUSDT")
        assert isinstance(result, dict)
        assert 1 in result

    def test_symbol_case_insensitive(self, client):
        client._on_ticker({"data": {"symbol": "BTCUSDT", "lastPrice": "60000"}, "ts": 1})
        # запрашиваем в нижнем регистре — должен найти
        assert client.get_latest("ticker", "btcusdt") is not None


# ============================================================
# Группа 4 — is_healthy()
# ============================================================

@pytest.mark.unit
class TestIsHealthy:
    def test_not_connected_is_unhealthy(self, client):
        assert client._connected is False
        assert client.is_healthy() is False

    def test_connected_no_messages_is_healthy(self, client):
        # На старте даём шанс
        client._connected = True
        client._last_message_ts = 0
        assert client.is_healthy() is True

    def test_connected_recent_message_is_healthy(self, client):
        client._connected = True
        client._last_message_ts = time.time() - 1.0  # 1 сек назад
        assert client.is_healthy() is True

    def test_connected_old_message_is_unhealthy(self, client):
        client._connected = True
        client._last_message_ts = time.time() - 120.0  # 2 минуты назад
        assert client.is_healthy() is False


# ============================================================
# Группа 5 — Singleton lifecycle
# ============================================================

@pytest.mark.unit
class TestSingletonLifecycle:
    @pytest.mark.asyncio
    async def test_get_websocket_raises_if_not_initialized(self):
        with pytest.raises(WebSocketNotInitialized):
            get_websocket()

    @pytest.mark.asyncio
    async def test_init_websocket_disabled_returns_none(self):
        with patch.object(ws_module, "get_settings") as mock_get:
            fake_settings = MagicMock()
            fake_settings.bybit_ws_enabled = False
            mock_get.return_value = fake_settings

            result = await init_websocket()
            assert result is None
            assert ws_module._ws_client is None

    @pytest.mark.asyncio
    async def test_init_websocket_creates_client(self):
        with patch.object(ws_module, "get_settings") as mock_get, \
             patch.object(BybitWebSocketClient, "start", new_callable=AsyncMock) as mock_start:
            fake_settings = MagicMock()
            fake_settings.bybit_ws_enabled = True
            fake_settings.allowed_symbols_list = ["BTCUSDT"]
            fake_settings.bybit_testnet = True
            fake_settings.bybit_ws_orderbook_depth = 50
            fake_settings.bybit_ws_kline_intervals = "1,15"
            fake_settings.bybit_ws_reconnect_base_delay_sec = 1.0
            fake_settings.bybit_ws_reconnect_max_delay_sec = 60.0
            fake_settings.bybit_ws_ping_interval_sec = 20
            mock_get.return_value = fake_settings

            result = await init_websocket()
            assert result is not None
            assert ws_module._ws_client is result
            mock_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_websocket_resets_singleton(self):
        # Сначала проинициализируем (с мокнутым start)
        with patch.object(BybitWebSocketClient, "start", new_callable=AsyncMock), \
             patch.object(BybitWebSocketClient, "stop", new_callable=AsyncMock) as mock_stop, \
             patch.object(ws_module, "get_settings") as mock_get:
            fake_settings = MagicMock()
            fake_settings.bybit_ws_enabled = True
            fake_settings.allowed_symbols_list = ["BTCUSDT"]
            fake_settings.bybit_testnet = True
            fake_settings.bybit_ws_orderbook_depth = 50
            fake_settings.bybit_ws_kline_intervals = "1"
            fake_settings.bybit_ws_reconnect_base_delay_sec = 1.0
            fake_settings.bybit_ws_reconnect_max_delay_sec = 60.0
            fake_settings.bybit_ws_ping_interval_sec = 20
            mock_get.return_value = fake_settings

            await init_websocket()
            assert ws_module._ws_client is not None

            await close_websocket()
            assert ws_module._ws_client is None
            mock_stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_websocket_no_op_if_not_initialized(self):
        # Не должен падать если клиент уже None
        await close_websocket()
        assert ws_module._ws_client is None


# ============================================================
# Группа 6 — start / stop
# ============================================================

@pytest.mark.unit
class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self, client):
        with patch.object(client, "_supervisor_loop", new_callable=AsyncMock):
            await client.start()
            assert client._task is not None
            # cleanup
            client._stop_flag.set()
            await asyncio.wait_for(client._task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_start_twice_warns_and_skips(self, client):
        with patch.object(client, "_supervisor_loop", new_callable=AsyncMock):
            await client.start()
            first_task = client._task
            await client.start()
            assert client._task is first_task  # тот же таск
            # cleanup
            client._stop_flag.set()
            await asyncio.wait_for(client._task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_stop_sets_flag_and_clears_task(self, client):
        async def fake_loop():
            await client._stop_flag.wait()

        with patch.object(client, "_supervisor_loop", side_effect=fake_loop):
            await client.start()
            assert client._task is not None
            await client.stop()
            assert client._task is None
            assert client._connected is False


# ============================================================
# Группа 7 — Кастомные исключения
# ============================================================

@pytest.mark.unit
class TestExceptions:
    def test_websocket_not_initialized_is_subclass(self):
        assert issubclass(WebSocketNotInitialized, BybitWebSocketError)

    def test_bybit_websocket_error_is_exception(self):
        assert issubclass(BybitWebSocketError, Exception)