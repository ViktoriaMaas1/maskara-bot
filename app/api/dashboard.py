from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import secrets
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.database.db import get_sessionmaker
from app.database.models import SignalRow
from app.engines.order_flow.engine import (
    OrderFlowEngineNotInitialized,
    get_order_flow_engine,
)
from app.engines.news.engine import get_news_engine
from app.config import get_settings
from app.utils.redis_client import get_redis
from app.bybit.websocket_client import get_websocket

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ====================================================================
# GET /dashboard/stats - 24h агрегация для метрик-карточек
# ====================================================================

@router.get("/stats", summary="Статистика сигналов за 24ч")
async def get_dashboard_stats() -> JSONResponse:
    """24h агрегация для 4 метрик-карточек дашборда."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        sm = get_sessionmaker()
        async with sm() as session:
            total = await session.scalar(
                select(func.count()).select_from(SignalRow).where(
                    SignalRow.created_at >= cutoff
                )
            )
            action_rows = (await session.execute(
                select(SignalRow.action, func.count())
                .where(SignalRow.created_at >= cutoff)
                .group_by(SignalRow.action)
            )).all()
            strength_rows = (await session.execute(
                select(SignalRow.strength, func.count())
                .where(SignalRow.created_at >= cutoff)
                .group_by(SignalRow.strength)
            )).all()
            last_created = await session.scalar(
                select(func.max(SignalRow.created_at))
            )

        by_action = {a: c for a, c in action_rows}
        by_strength = {s: c for s, c in strength_rows}

        last_signal_ago_sec = None
        if last_created is not None:
            delta = datetime.now(timezone.utc) - last_created
            last_signal_ago_sec = int(delta.total_seconds())

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "total_24h": int(total or 0),
                "buy": int(by_action.get("BUY", 0)),
                "sell": int(by_action.get("SELL", 0)),
                "weak": int(by_strength.get("WEAK", 0)),
                "medium": int(by_strength.get("MEDIUM", 0)),
                "strong": int(by_strength.get("STRONG", 0)),
                "medium_strong": int(
                    by_strength.get("MEDIUM", 0) + by_strength.get("STRONG", 0)
                ),
                "last_signal_ago_sec": last_signal_ago_sec,
            },
        )
    except RuntimeError as e:
        logger.warning("DB not ready for /dashboard/stats: %s", e)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "db_not_initialized"},
        )
    except Exception as e:
        logger.exception("/dashboard/stats failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )
# ====================================================================
# GET /dashboard/orderflow/{symbol} - live snapshot метрик
# ====================================================================

@router.get("/orderflow/{symbol}", summary="Order Flow snapshot для дашборда")
async def get_dashboard_orderflow(symbol: str) -> JSONResponse:
    """Live snapshot order flow для символа (обёртка для дашборда)."""
    try:
        engine = get_order_flow_engine()
    except OrderFlowEngineNotInitialized:
        logger.warning("OrderFlowEngine not initialized for /dashboard/orderflow")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "symbol": symbol.upper(),
                "data_available": False,
                "reason": "engine_not_initialized",
            },
        )

    try:
        snapshot = await engine.get_snapshot(symbol.upper())
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=snapshot.model_dump(),
        )
    except Exception as e:
        logger.exception("/dashboard/orderflow failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )
# ====================================================================
# GET /dashboard/signals - последние сигналы + фильтр периода
# ====================================================================

_PERIOD_TO_HOURS = {"1h": 1, "24h": 24, "7d": 168}


def _signal_to_dict(row: SignalRow) -> dict:
    """Конвертирует SignalRow в JSON-сериализуемый dict для дашборда."""
    return {
        "id": str(row.id),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "symbol": row.symbol,
        "timestamp_ms": row.timestamp_ms,
        "action": row.action,
        "strength": row.strength,
        "score": float(row.score),
        "reasons": row.reasons or [],
    }


@router.get("/signals", summary="Последние сигналы для дашборда")
async def get_dashboard_signals(
    period: str = "24h",
    limit: int = 20,
) -> JSONResponse:
    """Последние сигналы с фильтром по периоду (1h / 24h / 7d).

    Период переводится в cutoff по created_at. limit ограничивает
    число строк (для таблицы дашборда - 20).
    """
    try:
        hours = _PERIOD_TO_HOURS.get(period, 24)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        limit = max(1, min(limit, 200))

        sm = get_sessionmaker()
        async with sm() as session:
            rows = (await session.execute(
                select(SignalRow)
                .where(SignalRow.created_at >= cutoff)
                .order_by(SignalRow.created_at.desc())
                .limit(limit)
            )).scalars().all()

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "period": period,
                "count": len(rows),
                "signals": [_signal_to_dict(r) for r in rows],
            },
        )
    except RuntimeError as e:
        logger.warning("DB not ready for /dashboard/signals: %s", e)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "reason": "db_not_initialized"},
        )
    except Exception as e:
        logger.exception("/dashboard/signals failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )
# ====================================================================
# GET /dashboard/cooldowns - активные cooldown'ы (вариант B: TTL ключей)
# ====================================================================

_COOLDOWN_SYMBOLS = ("BTCUSDT", "ETHUSDT")
_COOLDOWN_ACTIONS = ("BUY", "SELL")


@router.get("/cooldowns", summary="Активные cooldown'ы для дашборда")
async def get_dashboard_cooldowns() -> JSONResponse:
    """Активные cooldown'ы по парам symbol x action."""
    try:
        total = int(get_settings().signal_cooldown_sec)
        redis = get_redis()

        items = []
        for symbol in _COOLDOWN_SYMBOLS:
            for action in _COOLDOWN_ACTIONS:
                key = f"signal_cooldown:{symbol}:{action}"
                ttl = await redis.ttl(key)
                if ttl is None or ttl < 0:
                    remaining = 0
                    active = False
                else:
                    remaining = int(ttl)
                    active = remaining > 0

                progress = 0.0
                if active and total > 0:
                    progress = round((total - remaining) / total, 4)

                items.append({
                    "symbol": symbol,
                    "action": action,
                    "active": active,
                    "remaining_sec": remaining,
                    "ttl_total_sec": total,
                    "progress": progress,
                })

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"data_available": True, "cooldowns": items},
        )
    except Exception as e:
        logger.exception("/dashboard/cooldowns failed")
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"data_available": False, "reason": str(e), "cooldowns": []},
        )
# ====================================================================
# GET /dashboard/news-mood - текущий новостной фон (Stage 10 Phase 3)
# ====================================================================
@router.get("/news-mood", summary="Текущий агрегированный новостной фон")
async def get_dashboard_news_mood() -> JSONResponse:
    """Средний sentiment свежих новостей + параметры влияния на сигналы.

    Не пишет в БД - считает фон на лету из снапшота новостей,
    как это делает SignalGenerator в Phase 3. Никогда не падает.
    """
    s = get_settings()
    base = {
        "available": False,
        "mood": None,
        "items_used": 0,
        "label": "no data",
        "influence_enabled": s.news_signal_influence_enabled,
        "veto_threshold": s.news_signal_veto_threshold,
        "weight": s.news_signal_score_weight,
    }
    try:
        snap = await get_news_engine().get_snapshot(limit=s.news_signal_mood_items)
        if not snap.data_available or not snap.items:
            return JSONResponse(status_code=status.HTTP_200_OK, content=base)
        scores = [it.sentiment_score for it in snap.items
                  if it.sentiment_score is not None]
        if not scores:
            return JSONResponse(status_code=status.HTTP_200_OK, content=base)
        mood = sum(scores) / len(scores)
        if mood >= 0.3:
            label = "bullish"
        elif mood <= -0.3:
            label = "bearish"
        else:
            label = "neutral"
        base.update({
            "available": True,
            "mood": round(mood, 4),
            "items_used": len(scores),
            "label": label,
        })
        return JSONResponse(status_code=status.HTTP_200_OK, content=base)
    except Exception as e:  # noqa: BLE001
        logger.warning("/dashboard/news-mood failed: %s", e)
        return JSONResponse(status_code=status.HTTP_200_OK, content=base)


# ====================================================================
# GET /dashboard/health - расширенный health (API/PG/Redis/WS)
# ====================================================================

@router.get("/health", summary="Расширенный health для дашборда")
async def get_dashboard_health() -> JSONResponse:
    """Состояние компонентов: API, Postgres, Redis, WebSocket.

    Каждый компонент проверяется независимо. Всегда HTTP 200 -
    статусы внутри (ok / down / unknown), фронт раскрашивает сам.
    """
    components = {"api": "ok", "postgres": "unknown", "redis": "unknown", "websocket": "unknown"}

    # Postgres
    try:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(select(1))
        components["postgres"] = "ok"
    except Exception:
        logger.warning("health: postgres check failed", exc_info=True)
        components["postgres"] = "down"

    # Redis
    try:
        pong = await get_redis().ping()
        components["redis"] = "ok" if pong else "down"
    except Exception:
        logger.warning("health: redis check failed", exc_info=True)
        components["redis"] = "down"

    # WebSocket
    try:
        ws = get_websocket()
        components["websocket"] = "ok" if ws.connected else "down"
    except Exception:
        logger.warning("health: websocket check failed", exc_info=True)
        components["websocket"] = "unknown"

    all_ok = all(v == "ok" for v in components.values())

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "ok" if all_ok else "degraded",
            "components": components,
        },
    )

# CSE= Dashboard HTML page + Basic Auth (Stage 4)
from fastapi.responses import HTMLResponse
from pathlib import Path

_security = HTTPBasic(auto_error=False)


def verify_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> None:
    from app.config import get_settings
    settings = get_settings()
    user = settings.dashboard_user
    password = settings.dashboard_password.get_secret_value()
    if not user and not password:
        return
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Basic"})
    ok_u = secrets.compare_digest(credentials.username, user)
    ok_p = secrets.compare_digest(credentials.password, password)
    if not (ok_u and ok_p):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


# Apply auth to all dashboard routes
router.dependencies.append(Depends(verify_dashboard_auth))

_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


@router.get("", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page() -> HTMLResponse:
    try:
        return HTMLResponse(_DASHBOARD_HTML.read_text(encoding="utf-8"))
    except Exception as e:
        logger.exception("dashboard page read failed")
        return HTMLResponse(f"<h1>Dashboard unavailable</h1><p>{e}</p>", status_code=500)


# ---- Stage 9: liquidity endpoint for dashboard ----
from app.engines.liquidity.engine import (
    LiquidityEngineNotInitialized as _LiqNotInit,
    get_liquidity_engine as _get_liq_engine,
)


@router.get("/liquidity/{symbol}", summary="Liquidity snapshot dashboard")
async def get_dashboard_liquidity(symbol: str) -> JSONResponse:
    try:
        engine = _get_liq_engine()
    except _LiqNotInit:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"symbol": symbol.upper(), "data_available": False, "reason": "engine_not_initialized"},
        )
    try:
        snapshot = await engine.get_snapshot(symbol.upper())
        return JSONResponse(status_code=status.HTTP_200_OK, content=snapshot.model_dump())
    except Exception as e:
        logger.exception("/dashboard/liquidity failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )


# ---- Stage 10: news endpoint for dashboard ----
from app.engines.news.engine import (
    NewsEngineNotInitialized as _NewsNotInit,
    get_news_engine as _get_news_engine,
)


@router.get("/news", summary="News feed for dashboard")
async def get_dashboard_news(limit: int = 20) -> JSONResponse:
    try:
        engine = _get_news_engine()
    except _NewsNotInit:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"data_available": False, "reason": "engine_not_initialized", "items": []},
        )
    try:
        snapshot = await engine.get_snapshot(limit=limit)
        return JSONResponse(status_code=status.HTTP_200_OK, content=snapshot.model_dump())
    except Exception as e:
        logger.exception("/dashboard/news failed")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "error", "error": str(e)},
        )
