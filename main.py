"""
MASKARA AI Trading Bot — точка входа.

Запуск:
    uvicorn main:app --reload      (разработка)
    docker-compose up               (продакшн)

Что здесь происходит:
- Создаётся FastAPI app с метаданными
- Подключаются роутеры из app.api.*
- Регистрируется обработчик ошибок валидации (читаемые ошибки вместо дефолтных)
- Lifespan — место для инициализации/завершения (БД, Redis, WebSocket)

Stage 1: минимальная конфигурация. На следующих этапах сюда добавятся:
- Шаг 1.8: подключение Postgres / Redis в lifespan
- Stage 5: запуск Bybit WebSocket в lifespan
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import health, order_flow, webhook
from app.config import get_settings
from app.database.db import close_db, init_db
from app.utils.logging_config import setup_logging
from app.utils.redis_client import close_redis, init_redis
from app.bybit.websocket_client import close_websocket, init_websocket
from app.cache.market_cache import close_market_cache, init_market_cache
from app.cache.market_cache import get_market_cache
from app.engines.order_flow.engine import close_order_flow_engine, init_order_flow_engine

# КРИТИЧНО: настраиваем логирование ДО любых других импортов / создания app.
# Иначе ранние сообщения уйдут в дефолтный stderr без формата.
_settings = get_settings()
setup_logging(level=_settings.log_level.value)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------
# Lifespan — startup / shutdown хуки
# --------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Управление жизненным циклом приложения.

    Stage 1: только лог о старте/остановке.
    Дальше тут появится:
    - Шаг 1.8: пул соединений с Postgres, клиент Redis
    - Stage 5: подключение к Bybit WebSocket
    - Stage 13: запуск Telegram bot polling в фоне
    """
    settings = get_settings()
    logger.info(
        "MASKARA bot стартует",
        extra={
            "app_env": settings.app_env.value,
            "bybit_testnet": settings.bybit_testnet,
            "allowed_symbols": settings.allowed_symbols_list,
        },
    )

    # Защита: production + mainnet требуют явного и осознанного решения
    if settings.is_mainnet_allowed:
        logger.warning(
            "⚠️  MAINNET MODE АКТИВЕН — реальные деньги! "
            "Убедись что прошёл 100+ testnet сделок и 30 дней forward test."
        )

    # ---------- Подключаем зависимости ----------
    await init_redis()
    await init_db()
    await init_websocket()
    await init_market_cache()
    init_order_flow_engine(get_market_cache())

    yield  # ← здесь приложение работает

    # ---------- Корректно отключаемся ----------
    close_order_flow_engine()
    await close_market_cache()
    await close_websocket()
    await close_db()
    await close_redis()

    logger.info("MASKARA bot останавливается")


# --------------------------------------------------------------
# Создание приложения
# --------------------------------------------------------------
def create_app() -> FastAPI:
    """Factory-функция — упрощает тестирование (можно создавать изолированные app)."""
    settings = get_settings()

    app = FastAPI(
        title="MASKARA AI Trading Bot",
        description=(
            "Профессиональный AI trading bot для Bybit Futures. "
            "Stage 1: webhook сервер. Никакой торговли пока."
        ),
        version="0.1.0-stage1",
        lifespan=lifespan,
        # Документация Swagger UI на /docs, ReDoc на /redoc — удобно для отладки
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    # ---------- Роутеры ----------
    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(order_flow.router)

    # ---------- Обработчик ошибок валидации ----------
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """
        Делает ошибки валидации читаемыми.

        Дефолтный FastAPI ответ содержит ВСЁ payload что прислали —
        включая secret. Мы это вычищаем.
        """
        # Чистим: убираем 'secret' из логов и из ответа
        cleaned_errors = []
        for err in exc.errors():
            cleaned = dict(err)
            # input может содержать сам payload — секрет туда лучше не светить
            if isinstance(cleaned.get("input"), dict):
                cleaned["input"] = {
                    k: ("***" if k == "secret" else v)
                    for k, v in cleaned["input"].items()
                }
            cleaned_errors.append(cleaned)

        logger.warning(
            "Webhook validation failed",
            extra={"errors": cleaned_errors, "path": request.url.path},
        )

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "validation_error",
                "detail": cleaned_errors,
            },
        )

    return app


# Глобальный экземпляр для uvicorn / docker
app = create_app()
