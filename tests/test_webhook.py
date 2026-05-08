"""
Тесты webhook через httpx AsyncClient.

Проверяем полный пайплайн:
- Pydantic валидация
- Secret check
- Whitelist символов
- Rate limit
- Дедупликация
- Корректные HTTP статус коды
"""

from __future__ import annotations

import asyncio
import os

import pytest


pytestmark = pytest.mark.unit


# Тот же secret что в conftest.py
VALID_SECRET = "test_secret_super_long_and_random_xxx_yyy_zzz"


def _payload(**overrides) -> dict:
    base = {
        "secret": VALID_SECRET,
        "symbol": "BTCUSDT",
        "side": "BUY",
        "timeframe": "3m",
        "strategy": "liquidity_sweep",
    }
    base.update(overrides)
    return base


# ==============================================================
# Healthcheck
# ==============================================================

class TestHealth:

    async def test_health_returns_ok(self, app_with_fake_redis):
        resp = await app_with_fake_redis.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data

    async def test_readiness_with_working_deps(self, app_with_fake_redis):
        resp = await app_with_fake_redis.get("/health/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert data["checks"]["redis"] is True
        assert data["checks"]["postgres"] is True


# ==============================================================
# Successful webhook
# ==============================================================

class TestWebhookAccepted:

    async def test_valid_signal_accepted(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post("/webhook", json=_payload())
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "accepted"
        assert "BTCUSDT" in data["message"]
        assert "BUY" in data["message"]
        # request_id всегда есть
        assert data["request_id"]

    async def test_request_id_from_header_preserved(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post(
            "/webhook",
            json=_payload(),
            headers={"X-Request-ID": "my-custom-id-12345"},
        )
        assert resp.status_code == 202
        assert resp.json()["request_id"] == "my-custom-id-12345"


# ==============================================================
# Secret check
# ==============================================================

class TestSecretCheck:

    async def test_invalid_secret_returns_401(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post(
            "/webhook",
            json=_payload(secret="wrong_secret_value_long_enough"),
        )
        assert resp.status_code == 401

    async def test_missing_secret_returns_422(self, app_with_fake_redis):
        bad = _payload()
        del bad["secret"]
        resp = await app_with_fake_redis.post("/webhook", json=bad)
        assert resp.status_code == 422

    async def test_secret_not_leaked_in_validation_error(self, app_with_fake_redis):
        """Если pydantic упал — secret не должен утечь в ответ."""
        resp = await app_with_fake_redis.post(
            "/webhook",
            json=_payload(secret="my_visible_secret_xxx", side="INVALID_SIDE"),
        )
        assert resp.status_code == 422
        body_text = resp.text
        # Secret не должен оказаться в ответе ни в каком виде
        assert "my_visible_secret_xxx" not in body_text


# ==============================================================
# Whitelist символов
# ==============================================================

class TestSymbolWhitelist:

    async def test_allowed_symbol_passes(self, app_with_fake_redis):
        for symbol in ["BTCUSDT", "ETHUSDT"]:
            resp = await app_with_fake_redis.post(
                "/webhook",
                json=_payload(symbol=symbol, strategy=f"strategy_{symbol}"),
            )
            assert resp.status_code == 202, f"{symbol} должен пройти"

    async def test_disallowed_symbol_returns_403(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post(
            "/webhook",
            json=_payload(symbol="DOGEUSDT"),
        )
        assert resp.status_code == 403


# ==============================================================
# Дедупликация
# ==============================================================

class TestDeduplication:

    async def test_duplicate_signal_returns_duplicate_status(self, app_with_fake_redis):
        # Первый — принят
        resp1 = await app_with_fake_redis.post("/webhook", json=_payload(strategy="dedup_test"))
        assert resp1.status_code == 202
        assert resp1.json()["status"] == "accepted"

        # Сразу повтор — duplicate
        resp2 = await app_with_fake_redis.post("/webhook", json=_payload(strategy="dedup_test"))
        # Возвращает 202 (приняли), но статус = duplicate
        assert resp2.status_code == 202
        assert resp2.json()["status"] == "duplicate"

    async def test_different_side_not_duplicate(self, app_with_fake_redis):
        await app_with_fake_redis.post("/webhook", json=_payload(side="BUY", strategy="diff_side"))
        resp = await app_with_fake_redis.post("/webhook", json=_payload(side="SELL", strategy="diff_side"))
        assert resp.json()["status"] == "accepted"


# ==============================================================
# Rate limit
# ==============================================================

class TestRateLimit:

    async def test_rate_limit_returns_429(self, app_with_fake_redis):
        # В conftest WEBHOOK_RATE_LIMIT_PER_MIN=5
        # Используем разные strategy чтобы не упереться в дедуп
        for i in range(5):
            resp = await app_with_fake_redis.post(
                "/webhook",
                json=_payload(strategy=f"rl_test_{i}"),
            )
            # Все 5 первых: либо accepted, либо duplicate — но не 429
            assert resp.status_code in (202, 409), \
                f"запрос #{i+1}: {resp.status_code}"

        # 6-й — 429
        resp = await app_with_fake_redis.post(
            "/webhook",
            json=_payload(strategy="rl_test_overflow"),
        )
        assert resp.status_code == 429
        # Retry-After заголовок должен быть
        assert "retry-after" in {h.lower() for h in resp.headers.keys()}


# ==============================================================
# Pydantic валидация на HTTP уровне
# ==============================================================

class TestHttpValidation:

    async def test_invalid_json_returns_422(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post(
            "/webhook",
            content="not a json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422

    async def test_invalid_side_returns_422(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post("/webhook", json=_payload(side="HOLD"))
        assert resp.status_code == 422

    async def test_extra_field_returns_422(self, app_with_fake_redis):
        resp = await app_with_fake_redis.post(
            "/webhook",
            json=_payload(unknown_field="hack_attempt"),
        )
        assert resp.status_code == 422
