"""
Webhook endpoint — приём сигналов от TradingView.

Stage 1 — что делаем:
- Проверяем secret (constant-time сравнение от timing-атак)
- Rate limit по IP (защита от флуда)
- Дедупликация (защита от повторов)
- Проверяем что символ в whitelist
- Логируем событие
- Возвращаем accepted

Stage 1 — что НЕ делаем (это в следующих этапах):
- Не торгуем (Stage 4)
- Не считаем scores (Stage 10)
- Не сохраняем в БД (Шаг 1.8 + Stage 11)

ВАЖНО (из задания):
TradingView signal сам по себе НЕ является причиной для сделки.
Этот endpoint только ЗАПУСКАЕТ дальнейшую проверку.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.dependencies import RequestIdDep, SettingsDep
from app.api.schemas import (
    ErrorResponse,
    WebhookRequest,
    WebhookResponse,
    WebhookStatus,
)
from app.utils.protection import Deduplicator, RateLimiter
from app.utils.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


# --------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------

def _verify_secret(provided: str, expected: str) -> bool:
    """
    Constant-time сравнение секретов.
    hmac.compare_digest всегда тратит одинаковое время — защита от timing-атак.
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _get_client_ip(request: Request) -> str:
    """
    Получает реальный IP клиента.

    Если бот за прокси (nginx, Cloudflare) — учитываем X-Forwarded-For.
    Если нет — берём прямой адрес.

    ВНИМАНИЕ: X-Forwarded-For доверять можно только от своего прокси.
    Иначе атакующий подделает заголовок и обойдёт rate limit.
    На своём VPS без прокси — используем request.client.host.
    """
    # На прод-сервере с nginx или CF можно раскомментировать:
    # forwarded = request.headers.get("X-Forwarded-For")
    # if forwarded:
    #     return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------
# POST /webhook
# --------------------------------------------------------------

@router.post(
    "",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"model": ErrorResponse, "description": "Неверный secret"},
        403: {"model": ErrorResponse, "description": "Символ не разрешён"},
        409: {"model": WebhookResponse, "description": "Дубликат сигнала"},
        422: {"model": ErrorResponse, "description": "Невалидный JSON"},
        429: {"model": ErrorResponse, "description": "Превышен rate limit"},
    },
    summary="Принять сигнал от TradingView",
)
async def receive_signal(
    request: Request,
    payload: WebhookRequest,
    settings: SettingsDep,
    request_id: RequestIdDep,
) -> WebhookResponse:
    """
    Принимает сигнал от TradingView. Порядок проверок имеет значение:

    1. Rate limit (отсекаем DDoS до любой работы)
    2. Secret (быстрая проверка авторизации)
    3. Whitelist символов
    4. Дедупликация (тяжелее — делаем последней)
    5. Логирование и accept
    """
    client_ip = _get_client_ip(request)
    redis = get_redis()

    # ------------------------------------------------------------------
    # 1. Rate limit по IP (sliding window 60 сек)
    # ------------------------------------------------------------------
    rate_limiter = RateLimiter(
        redis=redis,
        limit=settings.webhook_rate_limit_per_min,
        window_sec=60,
    )
    allowed, remaining = await rate_limiter.check(client_ip)
    if not allowed:
        logger.warning(
            "Webhook отклонён: rate limit",
            extra={
                "request_id": request_id,
                "ip": client_ip,
                "limit": settings.webhook_rate_limit_per_min,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({settings.webhook_rate_limit_per_min}/min)",
            headers={"Retry-After": "60"},
        )

    # ------------------------------------------------------------------
    # 2. Проверка secret (constant-time)
    # ------------------------------------------------------------------
    expected_secret = settings.webhook_secret.get_secret_value()
    if not _verify_secret(payload.secret.get_secret_value(), expected_secret):
        # ВАЖНО: не логируем сам secret — даже невалидный.
        logger.warning(
            "Webhook отклонён: невалидный secret",
            extra={
                "request_id": request_id,
                "ip": client_ip,
                "symbol": payload.symbol,
                "side": payload.side.value,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid secret",
        )

    # ------------------------------------------------------------------
    # 3. Whitelist символов
    # ------------------------------------------------------------------
    if payload.symbol not in settings.allowed_symbols_list:
        logger.warning(
            "Webhook отклонён: символ не в whitelist",
            extra={
                "request_id": request_id,
                "ip": client_ip,
                "symbol": payload.symbol,
                "allowed": settings.allowed_symbols_list,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Symbol {payload.symbol} not allowed. "
                   f"Allowed: {settings.allowed_symbols_list}",
        )

    # ------------------------------------------------------------------
    # 4. Дедупликация
    # ------------------------------------------------------------------
    deduplicator = Deduplicator(redis=redis, ttl_sec=settings.webhook_dedup_ttl_sec)
    dedup_key = payload.dedup_key()
    if await deduplicator.is_duplicate(dedup_key):
        logger.info(
            "Webhook дубликат — игнорируем",
            extra={
                "request_id": request_id,
                "ip": client_ip,
                "symbol": payload.symbol,
                "side": payload.side.value,
                "timeframe": payload.timeframe.value,
                "strategy": payload.strategy,
                "dedup_ttl": settings.webhook_dedup_ttl_sec,
            },
        )
        # Не выкидываем 4xx — это нормальная ситуация для TradingView
        # (alert может срабатывать дважды). Говорим что приняли но проигнорировали.
        return WebhookResponse(
            status=WebhookStatus.DUPLICATE,
            message=f"Duplicate signal within {settings.webhook_dedup_ttl_sec}s window",
            request_id=request_id,
        )

    # ------------------------------------------------------------------
    # 5. Принят. Логируем и отдаём ответ.
    # ------------------------------------------------------------------
    logger.info(
        "Webhook принят",
        extra={
            "request_id": request_id,
            "ip": client_ip,
            "symbol": payload.symbol,
            "side": payload.side.value,
            "timeframe": payload.timeframe.value,
            "strategy": payload.strategy,
            "support_zone": str(payload.support_zone) if payload.support_zone else None,
            "resistance_zone": str(payload.resistance_zone) if payload.resistance_zone else None,
            "rate_remaining": remaining,
        },
    )

    # ------------------------------------------------------------------
    # В Stage 2+ здесь будет:
    #   - сохранение в БД (webhook_signals)
    #   - запуск AI Decision Engine
    #   - передача в Risk Manager
    #   - выполнение через Execution Engine
    # ------------------------------------------------------------------

    return WebhookResponse(
        status=WebhookStatus.ACCEPTED,
        message=f"Signal {payload.side.value} {payload.symbol} {payload.timeframe.value} accepted",
        request_id=request_id,
    )
