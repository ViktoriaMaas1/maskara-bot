"""
MASKARA AI Trading Bot - entry point.
Stage 8: Signal Generator + Dashboard. Stage 9: Liquidity Engine. Stage 10: News Engine.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, Request, status
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
from app.engines.order_flow.engine import get_order_flow_engine
from app.engines.signals.cooldown import CooldownGate
from app.engines.signals.generator import SignalGenerator
from app.engines.signals.notifier import SignalNotifier
from app.workers.signal_worker import close_signal_worker, init_signal_worker
from app.database.db import get_sessionmaker
from app.utils.redis_client import get_redis
from app.api import signals as signals_api
from app.api import dashboard as dashboard_api

from app.engines.liquidity.engine import init_liquidity_engine
from app.api import liquidity as liquidity_api

# Stage 10: News Engine
from app.engines.news.engine import init_news_engine
from app.workers.news_worker import init_news_worker, close_news_worker

_settings = get_settings()
setup_logging(level=_settings.log_level.value)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logger.info(
        "MASKARA bot starting",
        extra={
            "app_env": settings.app_env.value,
            "bybit_testnet": settings.bybit_testnet,
            "allowed_symbols": settings.allowed_symbols_list,
        },
    )

    if settings.is_mainnet_allowed:
        logger.warning(
            "MAINNET MODE ACTIVE - real money! "
            "Make sure you passed 100+ testnet trades and 30 days forward test."
        )

    await init_redis()
    await init_db()
    await init_websocket()
    await init_market_cache()
    init_order_flow_engine(get_market_cache())

    init_liquidity_engine(get_market_cache())

    # Stage 10: News Engine + background worker
    _news_engine = init_news_engine()
    _news_worker = init_news_worker(_news_engine, interval_sec=300)
    await _news_worker.start()

    _signal_cooldown = CooldownGate(get_redis(), ttl_seconds=settings.signal_cooldown_sec)
    _signal_notifier = SignalNotifier()
    _signal_generator = SignalGenerator(
        session_factory=get_sessionmaker(),
        cooldown=_signal_cooldown,
        notifier=_signal_notifier,
    )
    _signal_worker = init_signal_worker(
        generator=_signal_generator,
        order_flow_engine=get_order_flow_engine(),
        symbols=settings.signal_symbols_list,
        interval_sec=settings.signal_polling_interval_sec,
    )
    await _signal_worker.start()

    yield

    await close_signal_worker()
    await close_news_worker()
    close_order_flow_engine()
    await close_market_cache()
    await close_websocket()
    await close_db()
    await close_redis()

    logger.info("MASKARA bot stopping")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="MASKARA AI Trading Bot",
        description="AI trading bot for Bybit Futures. Stage 8: signals + dashboard. Stage 9: liquidity. Stage 10: news.",
        version="0.10.0-stage10",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    app.include_router(health.router)
    app.include_router(webhook.router)
    app.include_router(order_flow.router)
    app.include_router(signals_api.router)
    app.include_router(liquidity_api.router)
    app.include_router(
        dashboard_api.router,
        dependencies=[Depends(dashboard_api.verify_dashboard_auth)],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        cleaned_errors = []
        for err in exc.errors():
            cleaned = dict(err)
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
            content={"error": "validation_error", "detail": cleaned_errors},
        )

    return app


app = create_app()
