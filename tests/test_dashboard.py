"""Тесты для dashboard API (Фаза 3).

Роутер тестируется изолированно: монтируется в чистое FastAPI-приложение
БЕЗ lifespan основного app (который требует init_db). Зависимости
(get_redis / get_order_flow_engine / get_sessionmaker) мокаются точечно
через monkeypatch. Сеть не используется.

asyncio_mode=auto -> @pytest.mark.asyncio не нужен.
"""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import dashboard


def _make_client() -> AsyncClient:
    app = FastAPI()
    app.include_router(dashboard.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ====================================================================
# /dashboard/cooldowns
# ====================================================================

async def test_cooldowns_structure(fake_redis, monkeypatch):
    """Cooldowns отдаёт 4 пары (2 символа x 2 действия)."""
    monkeypatch.setattr(dashboard, "get_redis", lambda: fake_redis)

    async with _make_client() as client:
        resp = await client.get("/dashboard/cooldowns")

    assert resp.status_code == 200
    data = resp.json()
    assert data["data_available"] is True
    cooldowns = data["cooldowns"]
    assert len(cooldowns) == 4
    pairs = {(c["symbol"], c["action"]) for c in cooldowns}
    assert ("BTCUSDT", "BUY") in pairs
    assert ("BTCUSDT", "SELL") in pairs
    assert ("ETHUSDT", "BUY") in pairs
    assert ("ETHUSDT", "SELL") in pairs


async def test_cooldowns_no_active_by_default(fake_redis, monkeypatch):
    """Без выставленных ключей все cooldown'ы неактивны."""
    monkeypatch.setattr(dashboard, "get_redis", lambda: fake_redis)

    async with _make_client() as client:
        resp = await client.get("/dashboard/cooldowns")

    data = resp.json()
    for c in data["cooldowns"]:
        assert c["active"] is False
        assert c["remaining_sec"] == 0
        assert c["progress"] == 0.0


async def test_cooldowns_active_when_key_set(fake_redis, monkeypatch):
    """Если ключ выставлен в Redis - соответствующая пара активна."""
    monkeypatch.setattr(dashboard, "get_redis", lambda: fake_redis)
    await fake_redis.set("signal_cooldown:BTCUSDT:SELL", "1", ex=30)

    async with _make_client() as client:
        resp = await client.get("/dashboard/cooldowns")

    data = resp.json()
    btc_sell = next(
        c for c in data["cooldowns"]
        if c["symbol"] == "BTCUSDT" and c["action"] == "SELL"
    )
    assert btc_sell["active"] is True
    assert btc_sell["remaining_sec"] > 0


# ====================================================================
# /dashboard/orderflow/{symbol}
# ====================================================================

async def test_orderflow_engine_not_initialized(monkeypatch):
    """Если движок не готов - 200 с data_available=False."""
    def _raise():
        raise dashboard.OrderFlowEngineNotInitialized()

    monkeypatch.setattr(dashboard, "get_order_flow_engine", _raise)

    async with _make_client() as client:
        resp = await client.get("/dashboard/orderflow/BTCUSDT")

    assert resp.status_code == 200
    data = resp.json()
    assert data["data_available"] is False
    assert data["symbol"] == "BTCUSDT"


# ====================================================================
# /dashboard/health
# ====================================================================

async def test_health_redis_ok(fake_redis, monkeypatch):
    """Health: Redis (fake) отвечает на ping -> redis ok, api ok."""
    monkeypatch.setattr(dashboard, "get_redis", lambda: fake_redis)

    async with _make_client() as client:
        resp = await client.get("/dashboard/health")

    assert resp.status_code == 200
    data = resp.json()
    assert "components" in data
    comps = data["components"]
    for key in ("api", "postgres", "redis", "websocket"):
        assert key in comps
    assert comps["api"] == "ok"
    assert comps["redis"] == "ok"


# ====================================================================
# /dashboard/stats (путь ошибки БД)
# ====================================================================

async def test_stats_db_error(monkeypatch):
    """При недоступной БД stats не падает: 503 или 500."""
    def _raise():
        raise RuntimeError("db not initialized")

    monkeypatch.setattr(dashboard, "get_sessionmaker", _raise)

    async with _make_client() as client:
        resp = await client.get("/dashboard/stats")

    assert resp.status_code in (503, 500)
    data = resp.json()
    assert data["status"] in ("unavailable", "error")