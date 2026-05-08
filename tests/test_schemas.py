"""
Тесты Pydantic схем — валидация payload без HTTP.

Покрываем:
- Правильный payload проходит
- Битый — отвергается с понятным описанием
- Нормализация (lowercase strategy, uppercase symbol)
- dedup_key детерминированный
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.api.schemas import Side, Timeframe, WebhookRequest


pytestmark = pytest.mark.unit


# ==============================================================
# Базовый валидный payload — правим под каждый тест
# ==============================================================

def _valid_payload(**overrides) -> dict:
    base = {
        "secret": "any_secret_string",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "timeframe": "3m",
        "strategy": "liquidity_sweep",
    }
    base.update(overrides)
    return base


# ==============================================================
# Happy path
# ==============================================================

class TestValidPayloads:

    def test_minimum_required_fields(self):
        req = WebhookRequest(**_valid_payload())
        assert req.symbol == "BTCUSDT"
        assert req.side == Side.BUY
        assert req.timeframe == Timeframe.M3
        assert req.strategy == "liquidity_sweep"

    def test_with_zones(self):
        req = WebhookRequest(**_valid_payload(
            support_zone="100000.50",
            resistance_zone="105000.123456",
        ))
        # Decimal сохраняет точность — float бы потерял
        assert req.support_zone == Decimal("100000.50")
        assert req.resistance_zone == Decimal("105000.123456")

    def test_zones_optional(self):
        req = WebhookRequest(**_valid_payload())
        assert req.support_zone is None
        assert req.resistance_zone is None


# ==============================================================
# Нормализация
# ==============================================================

class TestNormalization:

    def test_symbol_uppercase(self):
        req = WebhookRequest(**_valid_payload(symbol="btcusdt"))
        assert req.symbol == "BTCUSDT"

    def test_symbol_strips_whitespace(self):
        req = WebhookRequest(**_valid_payload(symbol="  ETHUSDT  "))
        assert req.symbol == "ETHUSDT"

    def test_strategy_normalized(self):
        req = WebhookRequest(**_valid_payload(strategy="Liquidity Sweep"))
        # 'Liquidity Sweep' → 'liquidity_sweep'
        assert req.strategy == "liquidity_sweep"

    def test_side_lowercase_rejected(self):
        # Side это enum BUY/SELL — нижний регистр не пройдёт
        # (это правильно — TradingView должен присылать BUY/SELL)
        with pytest.raises(ValidationError):
            WebhookRequest(**_valid_payload(side="buy"))


# ==============================================================
# Невалидные payload
# ==============================================================

class TestInvalidPayloads:

    def test_missing_secret(self):
        payload = _valid_payload()
        del payload["secret"]
        with pytest.raises(ValidationError) as exc:
            WebhookRequest(**payload)
        assert "secret" in str(exc.value)

    def test_missing_symbol(self):
        payload = _valid_payload()
        del payload["symbol"]
        with pytest.raises(ValidationError):
            WebhookRequest(**payload)

    def test_invalid_side(self):
        with pytest.raises(ValidationError):
            WebhookRequest(**_valid_payload(side="HOLD"))

    def test_invalid_timeframe(self):
        # 5m нет в нашем enum (только 1m/3m/15m/1h)
        with pytest.raises(ValidationError):
            WebhookRequest(**_valid_payload(timeframe="5m"))

    def test_extra_fields_forbidden(self):
        # extra="forbid" — лишние поля = ошибка (защита от мусора)
        with pytest.raises(ValidationError) as exc:
            WebhookRequest(**_valid_payload(unknown_field="value"))
        assert "unknown_field" in str(exc.value).lower() or "extra" in str(exc.value).lower()

    def test_negative_zone_rejected(self):
        # gt=0 в схеме
        with pytest.raises(ValidationError):
            WebhookRequest(**_valid_payload(support_zone="-100"))

    def test_zero_zone_rejected(self):
        with pytest.raises(ValidationError):
            WebhookRequest(**_valid_payload(support_zone="0"))


# ==============================================================
# dedup_key — детерминированный
# ==============================================================

class TestDedupKey:

    def test_same_payload_same_key(self):
        req1 = WebhookRequest(**_valid_payload())
        req2 = WebhookRequest(**_valid_payload())
        assert req1.dedup_key() == req2.dedup_key()

    def test_different_side_different_key(self):
        buy = WebhookRequest(**_valid_payload(side="BUY"))
        sell = WebhookRequest(**_valid_payload(side="SELL"))
        assert buy.dedup_key() != sell.dedup_key()

    def test_different_timeframe_different_key(self):
        m3 = WebhookRequest(**_valid_payload(timeframe="3m"))
        h1 = WebhookRequest(**_valid_payload(timeframe="1h"))
        assert m3.dedup_key() != h1.dedup_key()

    def test_dedup_key_format(self):
        req = WebhookRequest(**_valid_payload())
        key = req.dedup_key()
        # должны увидеть все составляющие
        assert "BTCUSDT" in key
        assert "BUY" in key
        assert "3m" in key
        assert "liquidity_sweep" in key
        assert key.startswith("webhook:dedup:")


# ==============================================================
# Secret не светится в repr
# ==============================================================

class TestSecretSafety:

    def test_secret_not_in_repr(self):
        """SecretStr должен скрывать значение при repr/str."""
        req = WebhookRequest(**_valid_payload(secret="my_super_secret"))
        # SecretStr.__repr__ не показывает значение
        assert "my_super_secret" not in repr(req)
        assert "my_super_secret" not in str(req)

    def test_secret_accessible_when_needed(self):
        req = WebhookRequest(**_valid_payload(secret="my_super_secret"))
        # Достать значение можно только явно
        assert req.secret.get_secret_value() == "my_super_secret"
