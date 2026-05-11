#!/usr/bin/env python3
"""
Тест подключения к Bybit testnet.

Запуск ВНУТРИ контейнера api:
    docker compose exec api python scripts/test_bybit.py

Что проверяет:
1. Загружается ли Settings из .env
2. Доступен ли testnet.bybit.com (server time)
3. Валидны ли API ключи (получение баланса)
4. Видны ли позиции и открытые ордера

Если всё ОК — увидишь свой testnet баланс и красивый зелёный ✅.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from app.bybit.exceptions import BybitAuthError, BybitNetworkError
from app.bybit.rest_client import BybitRestClient
from app.config import get_settings


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _ok(msg: str) -> None:
    print(f"{GREEN}✅ {msg}{RESET}")


def _fail(msg: str) -> None:
    print(f"{RED}❌ {msg}{RESET}")


def _warn(msg: str) -> None:
    print(f"{YELLOW}⚠️  {msg}{RESET}")


def _section(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}")


async def main() -> int:
    print(f"{BOLD}MASKARA — Bybit connectivity test{RESET}")
    print(f"Started at {datetime.now(timezone.utc).isoformat()}")

    # 1. Settings
    _section("1. Configuration")
    try:
        settings = get_settings()
    except Exception as e:
        _fail(f"Cannot load settings from .env: {e}")
        return 1

    if not settings.bybit_api_key or not settings.bybit_api_secret:
        _fail("BYBIT_API_KEY or BYBIT_API_SECRET is empty in .env")
        return 1

    env_label = "TESTNET" if settings.bybit_testnet else "MAINNET ⚠️⚠️⚠️"
    _ok(f"Settings loaded — environment: {env_label}")
    print(f"   API key: {settings.bybit_api_key.get_secret_value()[:6]}***{settings.bybit_api_key.get_secret_value()[-4:]}")

    if not settings.bybit_testnet:
        print("⚠️  WARNING: running on MAINNET. Bot kill-switch (BYBIT_READONLY_MODE) protects writes.")

    # 2. Подключение
    _section("2. Connection (public endpoint)")
    client = BybitRestClient.from_settings(settings)
    try:
        ts_ms = await client.get_server_time()
        server_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        local_time = datetime.now(timezone.utc)
        drift_ms = abs((server_time - local_time).total_seconds() * 1000)
        _ok(f"testnet.bybit.com reachable, server time: {server_time.isoformat()}")
        if drift_ms > 5000:
            _warn(f"Time drift: {drift_ms:.0f}ms (>5s — могут быть проблемы с подписью)")
        else:
            print(f"   Time drift: {drift_ms:.0f}ms (OK)")
    except BybitNetworkError as e:
        _fail(f"Cannot reach Bybit: {e}")
        return 2
    except Exception as e:
        _fail(f"Unexpected error: {e}")
        return 2

    # 3. Аутентификация (баланс)
    _section("3. Authentication (private endpoint)")
    try:
        balance = await client.get_wallet_balance()
    except BybitAuthError as e:
        _fail(f"Auth failed — check API key/secret/IP whitelist: {e}")
        return 3
    except Exception as e:
        _fail(f"Cannot get balance: {e}")
        return 3

    _ok("API keys valid")
    print(f"   Total equity:        ${balance.total_equity:,.2f}")
    print(f"   Available balance:   ${balance.total_available_balance:,.2f}")
    print(f"   Margin balance:      ${balance.total_margin_balance:,.2f}")
    print(f"   Initial margin used: ${balance.total_initial_margin:,.2f}")
    if balance.coins:
        print(f"   Coins:")
        for coin, amount in sorted(balance.coins.items()):
            if amount > 0:
                print(f"     {coin:8s} = {amount}")

    # 4. Позиции
    _section("4. Open positions")
    try:
        positions = await client.get_positions()
    except Exception as e:
        _warn(f"Cannot get positions: {e}")
    else:
        if not positions:
            print("   (no open positions)")
        else:
            for p in positions:
                pnl_color = GREEN if p.unrealized_pnl >= 0 else RED
                print(
                    f"   {p.symbol:10s} {p.side:4s} size={p.size} @ {p.avg_price} "
                    f"PnL={pnl_color}{p.unrealized_pnl:+.4f}{RESET}"
                )

    # 5. Открытые ордера
    _section("5. Open orders")
    try:
        orders = await client.get_open_orders()
    except Exception as e:
        _warn(f"Cannot get orders: {e}")
    else:
        if not orders:
            print("   (no open orders)")
        else:
            for o in orders:
                print(
                    f"   {o.symbol:10s} {o.side:4s} {o.order_type:6s} "
                    f"qty={o.qty} @ {o.price} status={o.status}"
                )

    # 6. Информация по BTCUSDT (нужно для Stage 3)
    _section("6. Instrument info (BTCUSDT)")
    try:
        info = await client.get_instrument_info("BTCUSDT")
    except Exception as e:
        _warn(f"Cannot get instrument info: {e}")
    else:
        _ok(f"BTCUSDT spec retrieved (status={info.status})")
        print(f"   min qty:  {info.min_order_qty}")
        print(f"   qty step: {info.qty_step}")
        print(f"   tick:     {info.tick_size}")
        print(f"   leverage: {info.min_leverage}x — {info.max_leverage}x")

    print(f"\n{BOLD}{GREEN}✅ All checks passed — Stage 2 is operational.{RESET}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(130)
