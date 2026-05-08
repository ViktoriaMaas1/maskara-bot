"""
Общие фикстуры pytest.

Что здесь:
- Подмена ENV для тестов (чтобы Settings прошёл валидацию)
- FakeRedis — in-memory имитация Redis
- TestClient для FastAPI

Принцип: тесты не должны требовать реального Postgres/Redis для запуска.
Тесты с реальной инфраструктурой = integration (помечены маркером).
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import AsyncIterator

import pytest

# ВАЖНО: настраиваем ENV до любого импорта app.* (Settings читает .env при импорте).
# Эти значения проходят все валидаторы из config.py.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("LOG_LEVEL", "WARNING")  # тише в тестах
os.environ.setdefault("WEBHOOK_SECRET", "test_secret_super_long_and_random_xxx_yyy_zzz")
os.environ.setdefault("ALLOWED_SYMBOLS", "BTCUSDT,ETHUSDT")
os.environ.setdefault("WEBHOOK_RATE_LIMIT_PER_MIN", "5")
os.environ.setdefault("WEBHOOK_DEDUP_TTL_SEC", "10")
os.environ.setdefault("POSTGRES_USER", "test_user")
os.environ.setdefault("POSTGRES_PASSWORD", "test_password_safe")
os.environ.setdefault("POSTGRES_DB", "test_db")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PASSWORD", "test_redis_safe")


# ==============================================================
# FakeRedis — минимальная in-memory имитация для тестов
# ==============================================================

class FakeRedis:
    """
    Реализует только те команды что нужны в protection.py:
    - pipeline + zremrangebyscore + zcard + zadd + expire
    - set с nx/ex
    - ping
    """

    def __init__(self):
        self.kv: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
        self.zset: dict[str, dict[str, float]] = defaultdict(dict)  # key -> {member: score}

    def _is_alive(self, key: str) -> bool:
        if key not in self.kv:
            return False
        _, expires_at = self.kv[key]
        if expires_at and expires_at < time.time():
            del self.kv[key]
            return False
        return True

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        pass

    async def set(self, key, value, ex=None, nx=False):
        if nx and self._is_alive(key):
            return None
        expires_at = time.time() + ex if ex else 0
        self.kv[key] = (str(value), expires_at)
        return True

    async def get(self, key):
        if not self._is_alive(key):
            return None
        return self.kv[key][0]

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: FakeRedis):
        self.redis = redis
        self.ops: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def zremrangebyscore(self, key, min_s, max_s):
        self.ops.append(("zremrange", key, min_s, max_s))
        return self

    def zcard(self, key):
        self.ops.append(("zcard", key))
        return self

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    async def execute(self):
        results = []
        for op in self.ops:
            kind = op[0]
            if kind == "zremrange":
                _, key, min_s, max_s = op
                z = self.redis.zset[key]
                to_del = [m for m, s in z.items() if min_s <= s <= max_s]
                for m in to_del:
                    del z[m]
                results.append(len(to_del))
            elif kind == "zcard":
                _, key = op
                results.append(len(self.redis.zset[key]))
            elif kind == "zadd":
                _, key, mapping = op
                self.redis.zset[key].update(mapping)
                results.append(len(mapping))
            elif kind == "expire":
                results.append(1)
        return results


# ==============================================================
# Фикстуры
# ==============================================================

@pytest.fixture
def fake_redis() -> FakeRedis:
    """Свежий FakeRedis для каждого теста — без утечек состояния."""
    return FakeRedis()


@pytest.fixture
async def app_with_fake_redis(fake_redis, monkeypatch) -> AsyncIterator:
    """
    FastAPI app с подменённым Redis. Postgres init заглушен.
    Использует httpx.AsyncClient — настоящий HTTP стек без сети.
    """
    # Подменяем функции инициализации до импорта main
    import app.utils.redis_client as rc
    monkeypatch.setattr(rc, "_redis_client", fake_redis)
    monkeypatch.setattr(rc, "init_redis", lambda: _async_return(fake_redis))
    monkeypatch.setattr(rc, "close_redis", _async_noop)

    # Postgres мокаем — в Stage 1 webhook её не использует
    import app.database.db as db_module
    monkeypatch.setattr(db_module, "init_db", _async_noop)
    monkeypatch.setattr(db_module, "close_db", _async_noop)
    monkeypatch.setattr(db_module, "healthcheck_db", lambda: _async_return(True))

    # Импортируем app только ПОСЛЕ моков
    from main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()

    # AsyncClient не запускает lifespan автоматически — делаем руками
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            yield client


async def _async_noop(*args, **kwargs):
    return None


async def _async_return(value):
    return value
