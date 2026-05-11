"""
Bybit REST Client — Stage 2

Async обёртка над pybit (синхронным SDK Bybit).

Архитектура:
- pybit работает синхронно, мы оборачиваем вызовы в asyncio.to_thread()
  чтобы не блокировать event loop FastAPI
- Все методы возвращают типизированные dataclasses, не сырые dict
- Ошибки Bybit → наши кастомные исключения
- Логирование с маскировкой секретов

Поддерживаемые операции (Stage 2):
- get_wallet_balance()         — баланс кошелька (UNIFIED account)
- get_positions(symbol)        — открытые позиции по символу или все
- get_open_orders(symbol)      — активные (неисполненные) ордера
- place_market_order(...)      — рыночный ордер на открытие/закрытие
- cancel_order(...)            — отменить активный ордер
- get_instrument_info(symbol)  — спецификация инструмента (min qty, tick size)
- get_server_time()            — для проверки связи + расчёта drift
- health_check()               — для /health/ready

Stage 3+ добавит:
- set_leverage(), set_position_mode()
- get_kline() / get_recent_trades()
- get_closed_pnl(), get_fee_rate()
- условные ордера (TP/SL, Stop)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from pybit.unified_trading import HTTP

from app.config import Settings, get_settings
from app.bybit.exceptions import (
    BybitAPIError,
    BybitAuthError,
    BybitNetworkError,
    BybitRateLimitError,
    BybitReadOnlyError,
)

logger = logging.getLogger(__name__)


# ============================================================
# Типизированные ответы (вместо сырых dict)
# ============================================================

@dataclass
class WalletBalance:
    """Баланс кошелька UNIFIED account."""

    total_equity: Decimal              # общий капитал в USD
    total_available_balance: Decimal   # доступно для торговли
    total_margin_balance: Decimal      # с учётом нереализованного PnL
    total_initial_margin: Decimal      # уже заблокировано под позиции
    total_wallet_balance: Decimal      # без учёта unrealized PnL
    coins: dict[str, Decimal] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Position:
    """Открытая позиция."""

    symbol: str
    side: str                  # "Buy" / "Sell" / "None" если позиции нет
    size: Decimal              # размер в контрактах
    avg_price: Decimal
    mark_price: Decimal
    leverage: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    position_value: Decimal
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class Order:
    """Активный ордер."""

    order_id: str
    order_link_id: str
    symbol: str
    side: str
    order_type: str            # "Market" / "Limit"
    qty: Decimal
    price: Decimal
    status: str                # "New" / "PartiallyFilled" / "Filled" / "Cancelled"
    created_time: int          # ms timestamp
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class InstrumentInfo:
    """Спецификация торгового инструмента."""

    symbol: str
    status: str                # "Trading" если можно торговать
    base_coin: str             # "BTC"
    quote_coin: str            # "USDT"
    min_order_qty: Decimal     # минимальный размер
    max_order_qty: Decimal
    qty_step: Decimal          # шаг размера (lot size)
    tick_size: Decimal         # шаг цены
    min_leverage: Decimal
    max_leverage: Decimal
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


# ============================================================
# Клиент
# ============================================================

class BybitRestClient:
    """
    Async клиент Bybit REST API.

    Использование:
        client = BybitRestClient.from_settings()
        balance = await client.get_wallet_balance()
        print(balance.total_equity)

    Singleton-стиль НЕ используется — каждый запрос FastAPI создаёт свой
    клиент через DI (см. app/api/dependencies.py). pybit.HTTP сам по себе
    thread-safe, но мы хотим явного контроля над временем жизни.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        recv_window: int = 5000,
        timeout: int = 10,
        readonly_mode: bool = True,
    ) -> None:
        if not api_key or not api_secret:
            raise BybitAuthError("API key/secret are empty")

        self._api_key = api_key
        self._testnet = testnet
        self._timeout = timeout
        self._readonly_mode = readonly_mode

        self._http = HTTP(
            testnet=testnet,
            api_key=api_key,
            api_secret=api_secret,
            recv_window=recv_window,
            timeout=timeout,
        )

        env = "testnet" if testnet else "MAINNET"
        logger.info(
            "Bybit REST client initialized",
            extra={"env": env, "api_key_prefix": api_key[:6] + "***"},
        )

    @classmethod
    def from_settings(cls, settings: Optional[Settings] = None) -> "BybitRestClient":
        """Создать клиент из Pydantic Settings."""
        s = settings or get_settings()
        return cls(
            api_key=s.bybit_api_key.get_secret_value(),
            api_secret=s.bybit_api_secret.get_secret_value(),
            testnet=s.bybit_testnet,
        readonly_mode=s.bybit_readonly_mode,
        )

    # --------------------------------------------------------
    # Внутреннее: async-обёртка для синхронного pybit
    # --------------------------------------------------------

    async def _call(self, method_name: str, /, **kwargs: Any) -> dict[str, Any]:
        """
        Вызвать метод pybit в отдельном потоке + распарсить ответ.

        Bybit V5 формат:
        {
            "retCode": 0,           # 0 = успех
            "retMsg": "OK",
            "result": {...},        # данные
            "retExtInfo": {},
            "time": 1234567890
        }
        """
        method = getattr(self._http, method_name, None)
        if method is None:
            raise BybitAPIError(f"pybit has no method: {method_name}")

        try:
            response = await asyncio.to_thread(method, **kwargs)
        except Exception as e:
            # сетевые ошибки, таймауты pybit
            logger.error(
                "Bybit network error",
                extra={"method": method_name, "error": str(e)[:200]},
            )
            raise BybitNetworkError(f"{method_name}: {e}") from e

        ret_code = response.get("retCode")
        ret_msg = response.get("retMsg", "")

        if ret_code == 0:
            return response.get("result", {})

        # маппинг известных ошибок
        if ret_code in (10003, 10004, 10005, 33004):
            # неверный API key, подпись, или просрочен
            raise BybitAuthError(f"[{ret_code}] {ret_msg}")
        if ret_code in (10006, 10018):
            # rate limit
            raise BybitRateLimitError(f"[{ret_code}] {ret_msg}")

        raise BybitAPIError(
            f"[{ret_code}] {ret_msg}",
            code=ret_code,
            method=method_name,
        )

    # --------------------------------------------------------
    # Публичный API: чтение
    # --------------------------------------------------------

    async def get_server_time(self) -> int:
        """Серверное время Bybit в миллисекундах. Без подписи."""
        result = await self._call("get_server_time")
        # формат: {"timeSecond": "...", "timeNano": "..."}
        return int(result.get("timeSecond", 0)) * 1000

    async def get_wallet_balance(
        self, account_type: str = "UNIFIED", coin: Optional[str] = None
    ) -> WalletBalance:
        """
        Баланс UNIFIED account (объединённого торгового счёта).

        coin: если указан, в .coins будет только этот coin
        """
        params: dict[str, Any] = {"accountType": account_type}
        if coin:
            params["coin"] = coin

        result = await self._call("get_wallet_balance", **params)
        accounts = result.get("list", [])
        if not accounts:
            raise BybitAPIError("Empty wallet balance response")

        acc = accounts[0]
        coins_balance: dict[str, Decimal] = {}
        for c in acc.get("coin", []):
            coins_balance[c["coin"]] = _dec(c.get("walletBalance", "0"))

        return WalletBalance(
            total_equity=_dec(acc.get("totalEquity", "0")),
            total_available_balance=_dec(acc.get("totalAvailableBalance", "0")),
            total_margin_balance=_dec(acc.get("totalMarginBalance", "0")),
            total_initial_margin=_dec(acc.get("totalInitialMargin", "0")),
            total_wallet_balance=_dec(acc.get("totalWalletBalance", "0")),
            coins=coins_balance,
            raw=acc,
        )

    async def get_positions(
        self, symbol: Optional[str] = None, category: str = "linear"
    ) -> list[Position]:
        """
        Открытые позиции. Если symbol не указан — все.

        category="linear" — USDT-perpetual фьючерсы (наш случай).
        """
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        else:
            # без symbol Bybit требует settleCoin
            params["settleCoin"] = "USDT"

        result = await self._call("get_positions", **params)
        positions: list[Position] = []
        for p in result.get("list", []):
            size = _dec(p.get("size", "0"))
            if size == 0:
                continue  # пустые позиции пропускаем
            positions.append(
                Position(
                    symbol=p["symbol"],
                    side=p.get("side", "None"),
                    size=size,
                    avg_price=_dec(p.get("avgPrice", "0")),
                    mark_price=_dec(p.get("markPrice", "0")),
                    leverage=_dec(p.get("leverage", "1")),
                    unrealized_pnl=_dec(p.get("unrealisedPnl", "0")),
                    realized_pnl=_dec(p.get("curRealisedPnl", "0")),
                    position_value=_dec(p.get("positionValue", "0")),
                    raw=p,
                )
            )
        return positions

    async def get_open_orders(
        self, symbol: Optional[str] = None, category: str = "linear"
    ) -> list[Order]:
        """Активные (неисполненные) ордера."""
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol
        else:
            params["settleCoin"] = "USDT"

        result = await self._call("get_open_orders", **params)
        orders: list[Order] = []
        for o in result.get("list", []):
            orders.append(
                Order(
                    order_id=o["orderId"],
                    order_link_id=o.get("orderLinkId", ""),
                    symbol=o["symbol"],
                    side=o["side"],
                    order_type=o.get("orderType", ""),
                    qty=_dec(o.get("qty", "0")),
                    price=_dec(o.get("price", "0")),
                    status=o.get("orderStatus", ""),
                    created_time=int(o.get("createdTime", 0)),
                    raw=o,
                )
            )
        return orders

    async def get_instrument_info(
        self, symbol: str, category: str = "linear"
    ) -> InstrumentInfo:
        """Спецификация инструмента — нужно для Stage 3 (расчёт размера ордера)."""
        result = await self._call(
            "get_instruments_info", category=category, symbol=symbol
        )
        items = result.get("list", [])
        if not items:
            raise BybitAPIError(f"Symbol {symbol} not found")

        item = items[0]
        lot = item.get("lotSizeFilter", {})
        price = item.get("priceFilter", {})
        leverage = item.get("leverageFilter", {})

        return InstrumentInfo(
            symbol=item["symbol"],
            status=item.get("status", ""),
            base_coin=item.get("baseCoin", ""),
            quote_coin=item.get("quoteCoin", ""),
            min_order_qty=_dec(lot.get("minOrderQty", "0")),
            max_order_qty=_dec(lot.get("maxOrderQty", "0")),
            qty_step=_dec(lot.get("qtyStep", "0")),
            tick_size=_dec(price.get("tickSize", "0")),
            min_leverage=_dec(leverage.get("minLeverage", "1")),
            max_leverage=_dec(leverage.get("maxLeverage", "1")),
            raw=item,
        )

    # --------------------------------------------------------
    # Публичный API: торговля (используется в Stage 4+)
    # --------------------------------------------------------

    async def place_market_order(
        self,
        symbol: str,
        side: str,                          # "Buy" / "Sell"
        qty: Decimal,
        category: str = "linear",
        reduce_only: bool = False,
        order_link_id: Optional[str] = None,
        position_idx: int = 0,              # 0 = One-Way mode
    ) -> dict[str, Any]:
        """
        Рыночный ордер.

        ⚠️ Stage 2 ТОЛЬКО реализует метод. Реальное использование — Stage 4
        после внедрения Risk Manager.
        """

        if self._readonly_mode:
            raise BybitReadOnlyError(
                "Write operation 'place_market_order' blocked: BYBIT_READONLY_MODE is enabled"
            )

        params: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "positionIdx": position_idx,
        }
        if reduce_only:
            params["reduceOnly"] = True
        if order_link_id:
            params["orderLinkId"] = order_link_id

        logger.info(
            "Placing market order",
            extra={
                "symbol": symbol, "side": side, "qty": str(qty),
                "reduce_only": reduce_only, "link_id": order_link_id,
            },
        )
        return await self._call("place_order", **params)

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
        category: str = "linear",
    ) -> dict[str, Any]:
        """Отменить ордер. Нужен либо order_id, либо order_link_id."""

        if self._readonly_mode:
            raise BybitReadOnlyError(
                "Write operation 'cancel_order' blocked: BYBIT_READONLY_MODE is enabled"
            )

        if not order_id and not order_link_id:
            raise ValueError("Either order_id or order_link_id required")

        params: dict[str, Any] = {"category": category, "symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id

        return await self._call("cancel_order", **params)

    # --------------------------------------------------------
    # Health check
    # --------------------------------------------------------

    async def health_check(self) -> bool:
        """
        True если Bybit доступен и наши API ключи валидны.

        Используется в /health/ready. Двухступенчатая проверка:
        1. get_server_time() — публичный endpoint, без подписи (проверка сети)
        2. get_wallet_balance() — приватный endpoint (проверка ключей)
        """
        try:
            await self.get_server_time()
        except Exception as e:
            logger.warning("Bybit reachability failed", extra={"error": str(e)[:200]})
            return False

        try:
            await self.get_wallet_balance()
        except BybitAuthError as e:
            logger.error("Bybit auth check failed", extra={"error": str(e)[:200]})
            return False
        except Exception as e:
            logger.warning("Bybit auth check error", extra={"error": str(e)[:200]})
            return False

        return True


# ============================================================
# Утилиты
# ============================================================

def _dec(value: Any, default: str = "0") -> Decimal:
    """Безопасное преобразование в Decimal. Bybit возвращает строки."""
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)
