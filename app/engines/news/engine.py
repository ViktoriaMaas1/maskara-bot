"""
News Engine - Stage 10, Phase 1.

Fetches RSS via fetcher, stores the feed in Redis (orjson), returns NewsSnapshot.
Singleton pattern like LiquidityEngine: init_news_engine() / get_news_engine().

Redis keys:
- news:items      - STRING (JSON array of NewsItem)
- news:last_fetch - STRING (unix ts of last refresh)
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import orjson

from app.utils.redis_client import get_redis

from . import fetcher
from .models import NewsItem, NewsSnapshot

logger = logging.getLogger(__name__)

_KEY_ITEMS = "news:items"
_KEY_LAST_FETCH = "news:last_fetch"
_TTL_SEC = 24 * 3600


class NewsEngineError(Exception):
    """Base News Engine error."""

    pass


class NewsEngineNotInitialized(NewsEngineError):
    """Singleton not initialized via init_news_engine()."""

    pass


_news_engine: Optional["NewsEngine"] = None


def init_news_engine(feeds: Dict[str, str] | None = None) -> "NewsEngine":
    """Initialize the News Engine singleton. Called in lifespan."""
    global _news_engine
    _news_engine = NewsEngine(feeds=feeds)
    logger.info("News Engine initialized")
    return _news_engine


def get_news_engine() -> "NewsEngine":
    """Get the singleton (raises if not initialized)."""
    if _news_engine is None:
        raise NewsEngineNotInitialized("Call init_news_engine() first")
    return _news_engine


class NewsEngine:
    """Collects RSS news and stores the feed in Redis."""

    def __init__(self, feeds: Dict[str, str] | None = None, max_items: int = 60) -> None:
        self._feeds = feeds or fetcher.DEFAULT_FEEDS
        self._max_items = max_items
        logger.info("NewsEngine created (feeds=%d)", len(self._feeds))

    async def refresh(self) -> int:
        """Fetch all feeds and overwrite the feed in Redis. Returns item count."""
        items = await fetcher.fetch_all(self._feeds)
        items = items[: self._max_items]

        payload = orjson.dumps([it.model_dump() for it in items])
        now = int(time.time())

        redis = get_redis()
        await redis.set(_KEY_ITEMS, payload, ex=_TTL_SEC)
        await redis.set(_KEY_LAST_FETCH, str(now), ex=_TTL_SEC)

        logger.info("NewsEngine.refresh: stored %d items", len(items))
        return len(items)

    async def get_snapshot(self, limit: int = 30) -> NewsSnapshot:
        """Read feed from Redis and return NewsSnapshot. Never raises."""
        try:
            redis = get_redis()
            raw = await redis.get(_KEY_ITEMS)
            if not raw:
                return NewsSnapshot(data_available=False)

            data = orjson.loads(raw)
            items = [NewsItem(**d) for d in data][: max(1, min(limit, 60))]

            last_raw = await redis.get(_KEY_LAST_FETCH)
            last_fetch = int(last_raw) if last_raw else 0

            return NewsSnapshot(
                data_available=True,
                count=len(items),
                items=items,
                last_fetch_ts=last_fetch,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("NewsEngine.get_snapshot failed: %s", e)
            return NewsSnapshot(data_available=False)
