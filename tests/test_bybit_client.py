"""
Тесты Bybit REST клиента.

Стратегия: мокаем pybit.HTTP полностью — никаких реальных HTTP-вызовов.
Проверяем что:
- ответы Bybit правильно парсятся в наши dataclasses
- ошибки по retCode превращаются в правильные исключения
- Decimal используется везде где деньги (никакого float!)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.bybit.exceptions import (
    BybitAPIError,
    BybitAuthError,
    BybitNetworkError,
    BybitRateLimitError,
)
from app.bybit.rest_client import BybitRestClient


# ============================================================
# Фикстуры
# ============================================================

@pytest.fixture
def mock_http():
    """Мок pybit.unified_trading.HTTP."""
    with patch("app.bybit.rest_client.HTTP") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield instance


@pytest.fixture
def client(mock_http):
    return BybitRestClient(
        api_key="test_key_1234567890",
        api_secret="test_secret_abcdef",
        testnet=True,
    )


# ============================================================
# Init
# ============================================================

@pytest.mark.unit
def test_init_requires_keys(mock_http):
    with pytest.raises(BybitAuthError):
        BybitRestClient(api_key="", api_secret="secret")
    with pytest.raises(BybitAuthError):
        BybitRestClient(api_key="key", api_secret="")


@pytest.mark.unit
def test_init_passes_testnet_flag(mock_http):
    BybitRestClient(api_key="k", api_secret="s", testnet=True)
    # pybit.HTTP получил testnet=True
    from app.bybit.rest_client import HTTP as patched_http_class
    patched_http_class.assert_called_once()
    _, kwargs = patched_http_class.call_args
    assert kwargs["testnet"] is True


# ============================================================
# Парсинг ответов
# ============================================================

@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_wallet_balance_parses_correctly(client, mock_http):
    mock_http.get_wallet_balance.return_value = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": [{
                "totalEquity": "10000.50",
                "totalAvailableBalance": "9500.00",
                "totalMarginBalance": "10000.50",
                "totalInitialMargin": "500.50",
                "totalWalletBalance": "10000.00",
                "coin": [
                    {"coin": "USDT", "walletBalance": "10000.00"},
                    {"coin": "BTC", "walletBalance": "0.5"},
                ],
            }]
        },
    }

    balance = await client.get_wallet_balance()

    assert balance.total_equity == Decimal("10000.50")
    assert balance.total_available_balance == Decimal("9500.00")
    assert balance.coins["USDT"] == Decimal("10000.00")
    assert balance.coins["BTC"] == Decimal("0.5")
    # все суммы — Decimal, не float!
    assert isinstance(balance.total_equity, Decimal)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_positions_skips_empty_positions(client, mock_http):
    mock_http.get_positions.return_value = {
        "retCode": 0,
        "result": {
            "list": [
                {  # пустая позиция — должна быть отфильтрована
                    "symbol": "ETHUSDT",
                    "side": "None",
                    "size": "0",
                    "avgPrice": "0",
                    "markPrice": "3000",
                    "leverage": "10",
                    "unrealisedPnl": "0",
                    "curRealisedPnl": "0",
                    "positionValue": "0",
                },
                {  # реальная позиция
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "size": "0.1",
                    "avgPrice": "50000",
                    "markPrice": "51000",
                    "leverage": "10",
                    "unrealisedPnl": "100",
                    "curRealisedPnl": "0",
                    "positionValue": "5000",
                },
            ]
        },
    }

    positions = await client.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "BTCUSDT"
    assert positions[0].size == Decimal("0.1")
    assert positions[0].unrealized_pnl == Decimal("100")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_instrument_info(client, mock_http):
    mock_http.get_instruments_info.return_value = {
        "retCode": 0,
        "result": {
            "list": [{
                "symbol": "BTCUSDT",
                "status": "Trading",
                "baseCoin": "BTC",
                "quoteCoin": "USDT",
                "lotSizeFilter": {
                    "minOrderQty": "0.001",
                    "maxOrderQty": "100",
                    "qtyStep": "0.001",
                },
                "priceFilter": {"tickSize": "0.1"},
                "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
            }]
        },
    }

    info = await client.get_instrument_info("BTCUSDT")
    assert info.symbol == "BTCUSDT"
    assert info.min_order_qty == Decimal("0.001")
    assert info.qty_step == Decimal("0.001")
    assert info.max_leverage == Decimal("100")


# ============================================================
# Маппинг ошибок
# ============================================================

@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("ret_code", [10003, 10004, 10005, 33004])
async def test_auth_error_mapped(client, mock_http, ret_code):
    mock_http.get_wallet_balance.return_value = {
        "retCode": ret_code,
        "retMsg": "Invalid API key",
        "result": {},
    }
    with pytest.raises(BybitAuthError):
        await client.get_wallet_balance()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("ret_code", [10006, 10018])
async def test_rate_limit_error_mapped(client, mock_http, ret_code):
    mock_http.get_wallet_balance.return_value = {
        "retCode": ret_code,
        "retMsg": "Too many requests",
        "result": {},
    }
    with pytest.raises(BybitRateLimitError):
        await client.get_wallet_balance()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_generic_api_error(client, mock_http):
    mock_http.get_wallet_balance.return_value = {
        "retCode": 99999,
        "retMsg": "Some error",
        "result": {},
    }
    with pytest.raises(BybitAPIError) as exc_info:
        await client.get_wallet_balance()
    assert exc_info.value.code == 99999


@pytest.mark.unit
@pytest.mark.asyncio
async def test_network_error_wrapped(client, mock_http):
    """Если pybit бросает исключение — мы оборачиваем в BybitNetworkError."""
    mock_http.get_wallet_balance.side_effect = ConnectionError("network down")
    with pytest.raises(BybitNetworkError):
        await client.get_wallet_balance()


# ============================================================
# Health check
# ============================================================

@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_ok(client, mock_http):
    mock_http.get_server_time.return_value = {
        "retCode": 0,
        "result": {"timeSecond": "1700000000"},
    }
    mock_http.get_wallet_balance.return_value = {
        "retCode": 0,
        "result": {"list": [{
            "totalEquity": "1000", "totalAvailableBalance": "1000",
            "totalMarginBalance": "1000", "totalInitialMargin": "0",
            "totalWalletBalance": "1000", "coin": [],
        }]},
    }
    assert await client.health_check() is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_returns_false_on_auth_error(client, mock_http):
    mock_http.get_server_time.return_value = {
        "retCode": 0,
        "result": {"timeSecond": "1700000000"},
    }
    mock_http.get_wallet_balance.return_value = {
        "retCode": 10003,
        "retMsg": "Invalid key",
        "result": {},
    }
    assert await client.health_check() is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_check_returns_false_on_network_error(client, mock_http):
    mock_http.get_server_time.side_effect = ConnectionError("down")
    assert await client.health_check() is False


# ============================================================
# Размещение ордера
# ============================================================

@pytest.mark.unit
@pytest.mark.asyncio
async def test_place_market_order_correct_params(client, mock_http):
    mock_http.place_order.return_value = {
        "retCode": 0,
        "result": {"orderId": "abc123"},
    }
    await client.place_market_order(
        symbol="BTCUSDT",
        side="Buy",
        qty=Decimal("0.01"),
        order_link_id="my-link-1",
    )
    mock_http.place_order.assert_called_once()
    _, kwargs = mock_http.place_order.call_args
    assert kwargs["symbol"] == "BTCUSDT"
    assert kwargs["side"] == "Buy"
    assert kwargs["orderType"] == "Market"
    assert kwargs["qty"] == "0.01"          # должно быть строкой!
    assert kwargs["orderLinkId"] == "my-link-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_order_requires_id(client, mock_http):
    with pytest.raises(ValueError):
        await client.cancel_order(symbol="BTCUSDT")  # ни order_id, ни link_id
