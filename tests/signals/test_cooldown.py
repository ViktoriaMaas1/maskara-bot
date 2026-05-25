"""Тесты CooldownGate — работа с Redis TTL.

Используем реальный Redis через init_redis().
Каждый тест работает с уникальным symbol для изоляции.
Cleanup ключей делается в фикстуре после теста.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio

from app.engines.signals.cooldown import (
    COOLDOWN_KEY_PREFIX,
    CooldownGate,
    DEFAULT_COOLDOWN_TTL_SEC,
)
from app.utils.redis_client import get_redis, init_redis


# ============================================================
# Фикстуры
# ============================================================

@pytest_asyncio.fixture
async def redis_client():
    """Боевой Redis (init_redis инициализирует глобальный _redis_client).

    Каждому тесту достаётся свежее подключение, готовое к работе.
    """
    await init_redis()
    yield get_redis()


@pytest_asyncio.fixture
async def cooldown_test_symbol(redis_client):
    """Уникальный символ + автоочистка всех его ключей после теста."""
    symbol = f"TEST_{uuid.uuid4().hex[:10]}"
    yield symbol
    # Cleanup — удаляем все ключи с этим символом для всех action
    for action in ("BUY", "SELL"):
        key = f"{COOLDOWN_KEY_PREFIX}:{symbol}:{action}"
        await redis_client.delete(key)


# ============================================================
# Тесты
# ============================================================

@pytest.mark.asyncio
async def test_is_allowed_initially_true(redis_client, cooldown_test_symbol):
    """На чистом символе cooldown свободен — is_allowed() возвращает True."""
    gate = CooldownGate(redis_client, ttl_seconds=DEFAULT_COOLDOWN_TTL_SEC)

    allowed = await gate.is_allowed(cooldown_test_symbol, "BUY")

    assert allowed is True


@pytest.mark.asyncio
async def test_mark_sent_blocks_next_request(redis_client, cooldown_test_symbol):
    """После mark_sent() сразу же is_allowed() → False."""
    gate = CooldownGate(redis_client, ttl_seconds=DEFAULT_COOLDOWN_TTL_SEC)

    # До mark_sent — свободно
    before = await gate.is_allowed(cooldown_test_symbol, "BUY")
    assert before is True

    # Ставим cooldown
    await gate.mark_sent(cooldown_test_symbol, "BUY")

    # После — заблокировано
    after = await gate.is_allowed(cooldown_test_symbol, "BUY")
    assert after is False

    # TTL должен быть в пределах (0, DEFAULT_COOLDOWN_TTL_SEC]
    ttl = await gate.ttl_remaining(cooldown_test_symbol, "BUY")
    assert 0 < ttl <= DEFAULT_COOLDOWN_TTL_SEC


@pytest.mark.asyncio
async def test_clear_releases_cooldown(redis_client, cooldown_test_symbol):
    """clear() снимает cooldown — следующий is_allowed() снова True."""
    gate = CooldownGate(redis_client, ttl_seconds=DEFAULT_COOLDOWN_TTL_SEC)

    await gate.mark_sent(cooldown_test_symbol, "BUY")
    assert (await gate.is_allowed(cooldown_test_symbol, "BUY")) is False

    await gate.clear(cooldown_test_symbol, "BUY")

    after_clear = await gate.is_allowed(cooldown_test_symbol, "BUY")
    assert after_clear is True


@pytest.mark.asyncio
async def test_different_symbols_independent(redis_client):
    """Cooldown на BTC не влияет на ETH — изоляция по символу."""
    gate = CooldownGate(redis_client, ttl_seconds=DEFAULT_COOLDOWN_TTL_SEC)
    btc = f"TEST_BTC_{uuid.uuid4().hex[:8]}"
    eth = f"TEST_ETH_{uuid.uuid4().hex[:8]}"

    try:
        await gate.mark_sent(btc, "BUY")

        # BTC заблокирован
        assert (await gate.is_allowed(btc, "BUY")) is False
        # ETH свободен — на него cooldown BTC не повлиял
        assert (await gate.is_allowed(eth, "BUY")) is True
    finally:
        await gate.clear(btc, "BUY")
        await gate.clear(eth, "BUY")


@pytest.mark.asyncio
async def test_different_actions_independent(redis_client, cooldown_test_symbol):
    """Cooldown BUY не блокирует SELL на том же символе."""
    gate = CooldownGate(redis_client, ttl_seconds=DEFAULT_COOLDOWN_TTL_SEC)

    await gate.mark_sent(cooldown_test_symbol, "BUY")

    # BUY заблокирован
    assert (await gate.is_allowed(cooldown_test_symbol, "BUY")) is False
    # SELL свободен
    assert (await gate.is_allowed(cooldown_test_symbol, "SELL")) is True


@pytest.mark.asyncio
async def test_ttl_expires_naturally(redis_client, cooldown_test_symbol):
    """После истечения TTL ключ исчезает автоматически — is_allowed() снова True."""
    # Короткий TTL = 1 секунда
    gate = CooldownGate(redis_client, ttl_seconds=1)

    await gate.mark_sent(cooldown_test_symbol, "BUY")
    assert (await gate.is_allowed(cooldown_test_symbol, "BUY")) is False

    # Ждём чуть больше TTL
    await asyncio.sleep(1.5)

    # Redis удалил ключ → снова allowed
    after_expire = await gate.is_allowed(cooldown_test_symbol, "BUY")
    assert after_expire is True