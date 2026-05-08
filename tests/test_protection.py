"""
Тесты защитных утилит.

Используем FakeRedis из conftest — настоящий Redis не нужен.
"""

from __future__ import annotations

import asyncio

import pytest

from app.utils.protection import Deduplicator, RateLimiter


pytestmark = pytest.mark.unit


# ==============================================================
# RateLimiter
# ==============================================================

class TestRateLimiter:

    async def test_allows_under_limit(self, fake_redis):
        rl = RateLimiter(fake_redis, limit=3, window_sec=60)
        for i in range(3):
            allowed, remaining = await rl.check("1.2.3.4")
            assert allowed, f"запрос #{i+1} должен пройти"

    async def test_blocks_over_limit(self, fake_redis):
        rl = RateLimiter(fake_redis, limit=3, window_sec=60)
        # Заполняем лимит
        for _ in range(3):
            await rl.check("1.2.3.4")
        # 4-й — отказ
        allowed, remaining = await rl.check("1.2.3.4")
        assert not allowed
        assert remaining == 0

    async def test_remaining_decreases(self, fake_redis):
        rl = RateLimiter(fake_redis, limit=5, window_sec=60)
        _, r1 = await rl.check("1.2.3.4")
        _, r2 = await rl.check("1.2.3.4")
        _, r3 = await rl.check("1.2.3.4")
        assert r1 > r2 > r3

    async def test_different_ips_independent(self, fake_redis):
        rl = RateLimiter(fake_redis, limit=2, window_sec=60)
        # Исчерпываем лимит для IP1
        await rl.check("1.1.1.1")
        await rl.check("1.1.1.1")
        allowed_ip1, _ = await rl.check("1.1.1.1")
        assert not allowed_ip1
        # IP2 имеет свой счётчик
        allowed_ip2, _ = await rl.check("2.2.2.2")
        assert allowed_ip2

    async def test_different_scopes_independent(self, fake_redis):
        """Разные scope = разные лимиты на одном IP."""
        rl = RateLimiter(fake_redis, limit=1, window_sec=60)
        a, _ = await rl.check("1.2.3.4", scope="webhook")
        b, _ = await rl.check("1.2.3.4", scope="other")
        assert a and b


# ==============================================================
# Deduplicator
# ==============================================================

class TestDeduplicator:

    async def test_first_signal_not_duplicate(self, fake_redis):
        dedup = Deduplicator(fake_redis, ttl_sec=10)
        is_dup = await dedup.is_duplicate("webhook:dedup:BTCUSDT:BUY:3m:strategy")
        assert not is_dup

    async def test_immediate_repeat_is_duplicate(self, fake_redis):
        dedup = Deduplicator(fake_redis, ttl_sec=10)
        key = "webhook:dedup:BTCUSDT:BUY:3m:strategy"
        await dedup.is_duplicate(key)  # первый
        is_dup = await dedup.is_duplicate(key)  # сразу повтор
        assert is_dup

    async def test_different_keys_independent(self, fake_redis):
        dedup = Deduplicator(fake_redis, ttl_sec=10)
        await dedup.is_duplicate("webhook:dedup:BTCUSDT:BUY:3m:strategy")
        # Другая сторона — не дубликат
        is_dup = await dedup.is_duplicate("webhook:dedup:BTCUSDT:SELL:3m:strategy")
        assert not is_dup

    async def test_expires_after_ttl(self, fake_redis):
        # TTL = 1 сек — реалистичный сценарий
        dedup = Deduplicator(fake_redis, ttl_sec=1)
        key = "webhook:dedup:test"
        await dedup.is_duplicate(key)
        # Сразу — дубликат
        assert await dedup.is_duplicate(key)
        # Ждём TTL
        await asyncio.sleep(1.1)
        # Не дубликат
        assert not await dedup.is_duplicate(key)
