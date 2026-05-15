"""
Bybit WebSocket Data Collector — Stage 5.

Подключается к публичным потокам Bybit V5 (linear):
- orderbook (depth N)
- publicTrade
- tickers
- kline (timeframes 1/3/15/60)
- liquidation

Архитектура:
- pybit.unified_trading.WebSocket работает СИНХРОННО через callbacks
- Мы оборачиваем его, накапливаем последние снимки в self._latest
- Главный цикл живёт в фоновой asyncio.Task, перезапускается на разрывах
- Singleton bybit_ws + init_websocket() / close_websocket() (паттерн как redis_client.py)

Что хранится в памяти (Stage 5, in-memory; Stage 6 переедет в Redis):
- self._latest["orderbook"][symbol] = {"b": [...], "a": [...], "ts": ...}
- self._latest["trade"][symbol]     = [{...}, ...] (последние N сделок)
- self._latest["ticker"][symbol]    = {...}
- self._latest["kline"][symbol][interval] = [{...}, ...] (последние N свечей)
- self._latest["liquidation"][symbol] = [{...}, ...] (последние N ликвидаций)

Health:
- is_healthy() → True если соединение живо и приходят сообщения
- last_message_ts — для health endpoint /health/websocket

TODO (Stage 6+):
- Перенести storage в Redis с TTL
- Добавить in-memory ring-buffer для history (для Order Flow Engine Stage 8)
- Приватные потоки (order/position/wallet) — Stage 11
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class BybitWebSocketError(Exception):
    """Базовая ошибка WebSocket клиента."""
    pass


class WebSocketNotInitialized(BybitWebSocketError):
    """Singleton bybit_ws не был инициализирован через init_websocket()."""
    pass


# ============================================================
# Константы
# ============================================================

# Сколько последних элементов хранить в памяти на символ.
# Stage 5 — минимум для health-checks; полная история придёт со Stage 6 (Market Cache в Redis).
_MAX_TRADES_PER_SYMBOL = 100
_MAX_KLINES_PER_TF = 50
_MAX_LIQUIDATIONS_PER_SYMBOL = 50

# Если за это время не пришло ни одного сообщения — считаем соединение мёртвым.
_HEARTBEAT_TIMEOUT_SEC = 60.0

# Через сколько подряд неуспешных попыток подключения слать Telegram alert.
_ALERT_AFTER_FAILED_ATTEMPTS = 5


# ============================================================
# Клиент
# ============================================================

class BybitWebSocketClient:
    """
    Async обёртка над синхронным pybit.unified_trading.WebSocket.

    Использование:
        client = BybitWebSocketClient.from_settings(symbols=["BTCUSDT", "ETHUSDT"])
        await client.start()
        ...
        snap = client.get_latest("orderbook", "BTCUSDT")
        ...
        await client.stop()
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        testnet: bool = True,
        orderbook_depth: int = 50,
        kline_intervals: Optional[list[int]] = None,
        reconnect_base_delay_sec: float = 1.0,
        reconnect_max_delay_sec: float = 60.0,
        ping_interval_sec: int = 20,
    ) -> None:
        if not symbols:
            raise ValueError("At least one symbol required")

        self._symbols = [s.upper() for s in symbols]
        self._testnet = testnet
        self._orderbook_depth = orderbook_depth
        self._kline_intervals = kline_intervals or [1, 3, 15, 60]
        self._reconnect_base_delay = reconnect_base_delay_sec
        self._reconnect_max_delay = reconnect_max_delay_sec
        self._ping_interval = ping_interval_sec

        # Хранилище последних снимков (in-memory, Stage 6 переедет в Redis)
        self._latest: dict[str, Any] = {
            "orderbook": {},     # {symbol: {"b": [...], "a": [...], "ts": ms}}
            "trade": {},         # {symbol: deque[{...}]}
            "ticker": {},        # {symbol: {...}}
            "kline": {},         # {symbol: {interval: deque[{...}]}}
            "liquidation": {},   # {symbol: deque[{...}]}
        }

        # Состояние подключения
        self._ws: Optional[Any] = None              # pybit WebSocket instance
        self._task: Optional[asyncio.Task] = None   # фоновая задача supervisor
        self._stop_flag = asyncio.Event()
        self._connected = False
        self._last_message_ts: float = 0.0
        self._failed_attempts = 0
        self._alert_sent = False

        env = "testnet" if testnet else "MAINNET"
        logger.info(
            "Bybit WebSocket client initialized: env=%s symbols=%s depth=%d intervals=%s",
            env, self._symbols, orderbook_depth, self._kline_intervals,
        )

    # --------------------------------------------------------
    # Factory из Settings
    # --------------------------------------------------------

    @classmethod
    def from_settings(cls, settings=None) -> "BybitWebSocketClient":
        s = settings or get_settings()
        intervals = [int(x.strip()) for x in s.bybit_ws_kline_intervals.split(",") if x.strip()]
        return cls(
            symbols=s.allowed_symbols_list,
            testnet=s.bybit_testnet,
            orderbook_depth=s.bybit_ws_orderbook_depth,
            kline_intervals=intervals,
            reconnect_base_delay_sec=s.bybit_ws_reconnect_base_delay_sec,
            reconnect_max_delay_sec=s.bybit_ws_reconnect_max_delay_sec,
            ping_interval_sec=s.bybit_ws_ping_interval_sec,
        )

    # --------------------------------------------------------
    # Public API
    # --------------------------------------------------------

    async def start(self) -> None:
        """Запускает фоновую задачу supervisor (с auto-reconnect)."""
        if self._task is not None and not self._task.done():
            logger.warning("WebSocket client already running")
            return
        self._stop_flag.clear()
        self._task = asyncio.create_task(self._supervisor_loop())
        logger.info("WebSocket supervisor task started")

    async def stop(self) -> None:
        """Сигнализирует supervisor остановиться и ждёт завершения."""
        self._stop_flag.set()
        if self._ws is not None:
            try:
                # pybit WebSocket.exit() — синхронный
                await asyncio.to_thread(self._safe_exit_ws)
            except Exception as e:
                logger.warning("Error closing WebSocket: %s", e)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("WebSocket task did not finish in 5s, cancelling")
                self._task.cancel()
            self._task = None
        self._connected = False
        logger.info("WebSocket client stopped")

    def is_healthy(self) -> bool:
        """
        True если:
        - подключение установлено
        - последнее сообщение пришло не более HEARTBEAT_TIMEOUT_SEC назад
        """
        if not self._connected:
            return False
        if self._last_message_ts == 0:
            # ещё ничего не приходило — даём шанс на старте
            return True
        return (time.time() - self._last_message_ts) < _HEARTBEAT_TIMEOUT_SEC

    @property
    def last_message_ts(self) -> float:
        return self._last_message_ts

    @property
    def connected(self) -> bool:
        return self._connected

    def get_latest(self, stream: str, symbol: str, interval: Optional[int] = None) -> Any:
        """
        Получить последний снимок для stream/symbol.

        stream: "orderbook" | "trade" | "ticker" | "kline" | "liquidation"
        interval: для kline (1/3/15/60), для остальных игнорируется

        Returns: dict / list / None если данных ещё нет
        """
        symbol = symbol.upper()
        if stream == "kline":
            tf_map = self._latest["kline"].get(symbol, {})
            if interval is None:
                return tf_map
            return list(tf_map.get(interval, []))
        bucket = self._latest.get(stream)
        if bucket is None:
            return None
        value = bucket.get(symbol)
        if isinstance(value, deque):
            return list(value)
        return value

    # --------------------------------------------------------
    # Supervisor loop — auto-reconnect с exponential backoff
    # --------------------------------------------------------

    async def _supervisor_loop(self) -> None:
        """Главный цикл: подключаемся, ждём пока живо, переподключаемся при сбое."""
        delay = self._reconnect_base_delay
        while not self._stop_flag.is_set():
            try:
                await asyncio.to_thread(self._connect_and_subscribe)
                # успешное подключение — сбрасываем backoff
                delay = self._reconnect_base_delay
                self._failed_attempts = 0
                if self._alert_sent:
                    logger.info("WebSocket reconnected after outage")
                    self._alert_sent = False
                self._connected = True

                # ждём пока соединение живо ИЛИ stop_flag
                while not self._stop_flag.is_set():
                    await asyncio.sleep(1.0)
                    if not self._is_ws_alive():
                        logger.warning("WebSocket appears dead, will reconnect")
                        break
                    if self._last_message_ts > 0 and (
                        time.time() - self._last_message_ts > _HEARTBEAT_TIMEOUT_SEC
                    ):
                        logger.warning(
                            "No messages for %.1fs, will reconnect",
                            time.time() - self._last_message_ts,
                        )
                        break

                # вышли из внутреннего цикла — закрываем ws перед reconnect
                self._connected = False
                try:
                    await asyncio.to_thread(self._safe_exit_ws)
                except Exception as e:
                    logger.debug("Error during reconnect-close: %s", e)

            except asyncio.CancelledError:
                logger.info("WebSocket supervisor cancelled")
                raise
            except Exception as e:
                self._connected = False
                self._failed_attempts += 1
                logger.exception(
                    "WebSocket supervisor error (attempt %d): %s",
                    self._failed_attempts, e,
                )

                # Telegram alert при затяжной недоступности
                if (
                    self._failed_attempts >= _ALERT_AFTER_FAILED_ATTEMPTS
                    and not self._alert_sent
                ):
                    await self._send_outage_alert(e)
                    self._alert_sent = True

            if self._stop_flag.is_set():
                break

            # экспоненциальный backoff с потолком
            logger.info("Reconnecting in %.1fs", delay)
            try:
                await asyncio.wait_for(self._stop_flag.wait(), timeout=delay)
                # дождались stop_flag — выходим
                break
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, self._reconnect_max_delay)

        self._connected = False
        logger.info("WebSocket supervisor exited")

    # --------------------------------------------------------
    # Подключение и подписки (синхронные, через to_thread)
    # --------------------------------------------------------

    def _connect_and_subscribe(self) -> None:
        """
        Создаёт pybit WebSocket и подписывается на все потоки.
        Вызывается через asyncio.to_thread (pybit синхронный).
        """
        from pybit.unified_trading import WebSocket

        self._ws = WebSocket(
            testnet=self._testnet,
            channel_type="linear",
            ping_interval=self._ping_interval,
            ping_timeout=10,
        )

        # Подписки. pybit принимает callback на каждый поток.
        for symbol in self._symbols:
            # orderbook
            self._ws.orderbook_stream(
                depth=self._orderbook_depth,
                symbol=symbol,
                callback=self._on_orderbook,
            )
            # publicTrade
            self._ws.trade_stream(
                symbol=symbol,
                callback=self._on_trade,
            )
            # tickers
            self._ws.ticker_stream(
                symbol=symbol,
                callback=self._on_ticker,
            )
            # klines — отдельная подписка на каждый таймфрейм
            for interval in self._kline_intervals:
                self._ws.kline_stream(
                    interval=str(interval),
                    symbol=symbol,
                    callback=self._on_kline,
                )
            # liquidations
            self._ws.all_liquidation_stream(
                symbol=symbol,
                callback=self._on_liquidation,
            )

        logger.info(
            "Subscribed to streams: symbols=%s, intervals=%s",
            self._symbols, self._kline_intervals,
        )

    def _is_ws_alive(self) -> bool:
        """Проверка живости pybit WebSocket."""
        if self._ws is None:
            return False
        try:
            # pybit имеет is_connected() метод
            return bool(self._ws.is_connected())
        except Exception:
            return False

    def _safe_exit_ws(self) -> None:
        """Безопасно закрыть pybit WebSocket."""
        if self._ws is None:
            return
        try:
            self._ws.exit()
        except Exception as e:
            logger.debug("Error in ws.exit(): %s", e)
        finally:
            self._ws = None

    # --------------------------------------------------------
    # Callbacks от pybit (вызываются в потоке pybit, НЕ в event loop)
    # --------------------------------------------------------

    def _on_orderbook(self, msg: dict[str, Any]) -> None:
        try:
            data = msg.get("data") or {}
            symbol = data.get("s") or msg.get("topic", "").split(".")[-1]
            if not symbol:
                return
            self._latest["orderbook"][symbol] = {
                "b": data.get("b", []),          # [[price, qty], ...]
                "a": data.get("a", []),
                "ts": msg.get("ts", 0),
                "u": data.get("u", 0),           # update id
                "seq": data.get("seq", 0),
            }
            self._last_message_ts = time.time()
        except Exception:
            logger.exception("Error processing orderbook message")

    def _on_trade(self, msg: dict[str, Any]) -> None:
        try:
            trades = msg.get("data") or []
            if not trades:
                return
            # data — список сделок
            for t in trades:
                symbol = t.get("s")
                if not symbol:
                    continue
                buf = self._latest["trade"].setdefault(
                    symbol, deque(maxlen=_MAX_TRADES_PER_SYMBOL)
                )
                buf.append({
                    "ts": t.get("T", 0),         # exec time ms
                    "side": t.get("S", ""),      # "Buy" / "Sell"
                    "price": t.get("p", "0"),
                    "qty": t.get("v", "0"),
                    "tradeId": t.get("i", ""),
                })
            self._last_message_ts = time.time()
        except Exception:
            logger.exception("Error processing trade message")

    def _on_ticker(self, msg: dict[str, Any]) -> None:
        try:
            data = msg.get("data") or {}
            symbol = data.get("symbol")
            if not symbol:
                return
            self._latest["ticker"][symbol] = {
                "lastPrice": data.get("lastPrice", "0"),
                "markPrice": data.get("markPrice", "0"),
                "indexPrice": data.get("indexPrice", "0"),
                "bid1Price": data.get("bid1Price", "0"),
                "ask1Price": data.get("ask1Price", "0"),
                "volume24h": data.get("volume24h", "0"),
                "turnover24h": data.get("turnover24h", "0"),
                "openInterest": data.get("openInterest", "0"),
                "fundingRate": data.get("fundingRate", "0"),
                "ts": msg.get("ts", 0),
            }
            self._last_message_ts = time.time()
        except Exception:
            logger.exception("Error processing ticker message")

    def _on_kline(self, msg: dict[str, Any]) -> None:
        try:
            klines = msg.get("data") or []
            topic = msg.get("topic", "")
            # topic = "kline.1.BTCUSDT"
            parts = topic.split(".")
            if len(parts) < 3:
                return
            interval = int(parts[1])
            symbol = parts[2]
            tf_buf = self._latest["kline"].setdefault(symbol, {}).setdefault(
                interval, deque(maxlen=_MAX_KLINES_PER_TF)
            )
            for k in klines:
                tf_buf.append({
                    "start": k.get("start", 0),
                    "end": k.get("end", 0),
                    "interval": k.get("interval", str(interval)),
                    "open": k.get("open", "0"),
                    "close": k.get("close", "0"),
                    "high": k.get("high", "0"),
                    "low": k.get("low", "0"),
                    "volume": k.get("volume", "0"),
                    "turnover": k.get("turnover", "0"),
                    "confirm": k.get("confirm", False),
                })
            self._last_message_ts = time.time()
        except Exception:
            logger.exception("Error processing kline message")

    def _on_liquidation(self, msg: dict[str, Any]) -> None:
        try:
            data = msg.get("data")
            if not data:
                return
            # liquidation может приходить как dict ИЛИ как list (зависит от endpoint)
            items = data if isinstance(data, list) else [data]
            for item in items:
                symbol = item.get("symbol") or item.get("s")
                if not symbol:
                    continue
                buf = self._latest["liquidation"].setdefault(
                    symbol, deque(maxlen=_MAX_LIQUIDATIONS_PER_SYMBOL)
                )
                buf.append({
                    "ts": item.get("updatedTime") or item.get("T", 0),
                    "side": item.get("side", ""),
                    "price": item.get("price", "0"),
                    "qty": item.get("size") or item.get("v", "0"),
                })
            self._last_message_ts = time.time()
        except Exception:
            logger.exception("Error processing liquidation message")

    # --------------------------------------------------------
    # Telegram alert
    # --------------------------------------------------------

    async def _send_outage_alert(self, error: Exception) -> None:
        """Послать alert через Stage 4 notifier при затяжной недоступности WS."""
        try:
            from app.telegram.notifier import notify_error
            await notify_error(
                action="websocket_supervisor",
                error=f"{type(error).__name__}: {error}",
                details=(
                    f"failed_attempts={self._failed_attempts}, "
                    f"symbols={','.join(self._symbols)}"
                ),
            )
        except Exception as e:
            logger.error("Failed to send outage alert: %s", e)


# ============================================================
# Singleton + lifecycle (паттерн как у app/utils/redis_client.py)
# ============================================================

_ws_client: Optional[BybitWebSocketClient] = None


async def init_websocket() -> Optional[BybitWebSocketClient]:
    """
    Создаёт singleton и запускает supervisor.

    Вызывается из main.py:lifespan() при старте приложения.
    Если bybit_ws_enabled = False — возвращает None (для тестов/dev).
    Если запуск падает — поднимает исключение (приложение упадёт на старте,
    что лучше чем работать частично).
    """
    global _ws_client

    settings = get_settings()
    if not settings.bybit_ws_enabled:
        logger.info("WebSocket disabled (bybit_ws_enabled=False), skipping init")
        return None

    client = BybitWebSocketClient.from_settings(settings)
    await client.start()
    _ws_client = client
    logger.info("WebSocket initialized")
    return client


async def close_websocket() -> None:
    """Останавливает supervisor. Вызывается из lifespan() при остановке."""
    global _ws_client
    if _ws_client is not None:
        try:
            await _ws_client.stop()
        except Exception as e:
            logger.warning("Error stopping WebSocket: %s", e)
        _ws_client = None
        logger.info("WebSocket closed")


def get_websocket() -> BybitWebSocketClient:
    """
    Получить инициализированный singleton.

    Используется кодом, которому нужны рыночные данные.
    Если не инициализирован — кидаем понятную ошибку.
    """
    if _ws_client is None:
        raise WebSocketNotInitialized(
            "WebSocket not initialized. "
            "Check lifespan() in main.py and bybit_ws_enabled in settings."
        )
    return _ws_client


# Для совместимости с импортами в стиле "from app.bybit.websocket_client import bybit_ws"
# bybit_ws будет None до init_websocket() — используй get_websocket() везде где можешь.
bybit_ws: Optional[BybitWebSocketClient] = None  # noqa: E305