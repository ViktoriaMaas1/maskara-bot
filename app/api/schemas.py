"""
Pydantic схемы для webhook от TradingView.

Принципы:
- Strict валидация: лишние поля отвергаются (extra="forbid")
- Все строки нормализуются (upper-case symbol/side/timeframe)
- Числовые зоны валидируются как Decimal — float'ы ненадёжны для цен
- Запрос/ответ разделены: WebhookRequest vs WebhookResponse

Формат входящего JSON (из задания):
{
    "secret": "MASKARA_SECRET",
    "symbol": "BTCUSDT",
    "side": "BUY",
    "timeframe": "3m",
    "strategy": "liquidity_sweep",
    "support_zone": 100000,
    "resistance_zone": 105000
}
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


# --------------------------------------------------------------
# Enums — защита от опечаток и валидных значений-самозванцев
# --------------------------------------------------------------

class Side(str, Enum):
    """Направление сделки. TradingView присылает в любом регистре — нормализуем."""
    BUY = "BUY"
    SELL = "SELL"


class Timeframe(str, Enum):
    """
    Таймфреймы из задания:
    - 1m  → точка входа
    - 3m  → setup
    - 15m → структура
    - 1h  → тренд
    """
    M1 = "1m"
    M3 = "3m"
    M15 = "15m"
    H1 = "1h"


# --------------------------------------------------------------
# Запрос: что приходит от TradingView
# --------------------------------------------------------------

class WebhookRequest(BaseModel):
    """
    Сигнал от TradingView.

    ВАЖНО: TradingView signal сам по себе НЕ является причиной для сделки.
    Он только запускает проверку — все данные пройдут через scoring engine,
    risk manager и AI decision engine.
    """

    model_config = ConfigDict(
        # Лишние поля = ошибка. Так мы сразу заметим если TradingView
        # начал присылать что-то новое или если кто-то пробует подставить мусор.
        extra="forbid",
        # Строки автоматически .strip() — защита от " BTCUSDT " с пробелами
        str_strip_whitespace=True,
    )

    # ---------- Аутентификация ----------
    secret: SecretStr = Field(
        ...,
        description="Секретный токен — сравнивается с WEBHOOK_SECRET из .env",
    )

    # ---------- Что торговать ----------
    symbol: str = Field(
        ...,
        min_length=3,
        max_length=20,
        description="Тикер Bybit, например BTCUSDT",
        examples=["BTCUSDT", "ETHUSDT"],
    )
    side: Side = Field(
        ...,
        description="BUY (long) или SELL (short)",
    )
    timeframe: Timeframe = Field(
        ...,
        description="Таймфрейм сигнала",
    )

    # ---------- Стратегия ----------
    strategy: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Имя стратегии (например, 'liquidity_sweep')",
    )

    # ---------- Зоны поддержки/сопротивления ----------
    # Decimal вместо float — float теряет точность на больших ценах
    # (BTC 100000.123456789 → float 100000.12345678901)
    support_zone: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Уровень поддержки (опционально)",
    )
    resistance_zone: Optional[Decimal] = Field(
        default=None,
        gt=0,
        description="Уровень сопротивления (опционально)",
    )

    # ---------- Нормализация ----------
    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, v: str) -> str:
        """BTCUSDT, btcusdt, BtcUsdt → BTCUSDT"""
        return v.upper().strip()

    @field_validator("strategy")
    @classmethod
    def _normalize_strategy(cls, v: str) -> str:
        """'Liquidity Sweep ' → 'liquidity_sweep'"""
        return v.strip().lower().replace(" ", "_")

    # ---------- Дополнительная логика ----------
    def dedup_key(self) -> str:
        """
        Ключ для дедупликации (используется в Redis).

        Один и тот же сигнал на одном таймфрейме за короткое время = дубликат.
        Пример: бот не должен открыть 5 BUY BTCUSDT 3m подряд.
        """
        return f"webhook:dedup:{self.symbol}:{self.side.value}:{self.timeframe.value}:{self.strategy}"


# --------------------------------------------------------------
# Ответы: что возвращаем клиенту (TradingView, тестам, дашборду)
# --------------------------------------------------------------

class WebhookStatus(str, Enum):
    """Возможные результаты обработки webhook."""
    ACCEPTED = "accepted"        # сигнал принят, ушёл в очередь обработки
    DUPLICATE = "duplicate"      # дубликат — проигнорирован
    REJECTED = "rejected"        # не прошёл валидацию (символ не разрешён, и т.п.)
    RATE_LIMITED = "rate_limited"  # превышен лимит


class WebhookResponse(BaseModel):
    """
    Ответ на webhook.

    Stage 1: только подтверждение приёма ("accepted").
    Дальше будем расширять (например, добавим AI decision когда появится).
    """

    model_config = ConfigDict(extra="forbid")

    status: WebhookStatus
    message: str = Field(..., max_length=500)
    request_id: str = Field(
        ...,
        description="UUID запроса — для трейсинга в логах",
    )


# --------------------------------------------------------------
# Ошибка
# --------------------------------------------------------------

class ErrorResponse(BaseModel):
    """Унифицированный формат ошибок API."""

    model_config = ConfigDict(extra="forbid")

    error: str
    detail: Optional[str] = None
    request_id: Optional[str] = None
