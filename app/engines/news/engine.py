"""
News Engine - Stage 10.
Phase 1: Fetches RSS via fetcher, stores the feed in Redis (orjson).
Phase 2: Optionally enriches items with sentiment (bullish/neutral/bearish)
         via SentimentAnalyzer (OpenAI), with a per-url Redis cache so each
         item is analyzed only once.
Singleton pattern like LiquidityEngine: init_news_engine() / get_news_engine().
Redis keys:
- news:items          - STRING (JSON array of NewsItem)
- news:last_fetch     - STRING (unix ts of last refresh)
- news:sent:<hash>    - STRING (JSON {sentiment, score}) per-url sentiment cache
"""
from __future__ import annotations
import hashlib
import logging
import time
from typing import Dict, List, Optional
import orjson
from app.config import get_settings
from app.utils.redis_client import get_redis
from . import fetcher
from .models import NewsItem, NewsSnapshot
from .sentiment import SentimentAnalyzer
logger = logging.getLogger(__name__)
_KEY_ITEMS = "news:items"
_KEY_LAST_FETCH = "news:last_fetch"
_KEY_SENT_PREFIX = "news:sent:"
_TTL_SEC = 24 * 3600
_SENT_TTL_SEC = 7 * 24 * 3600  # sentiment cache lives a week


class NewsEngineError(Exception):
    """Base News Engine error."""
    pass


class NewsEngineNotInitialized(NewsEngineError):
    """Singleton not initialized via init_news_engine()."""
    pass


_news_engine: Optional["NewsEngine"] = None


def init_news_engine(feeds: Dict[str, str] | None = None) -> "NewsEngine":
    """Initialize the News Engine singleton. Called in lifespan.

    Builds a SentimentAnalyzer from settings if news_sentiment_enabled is on
    and an OpenAI key is present; otherwise sentiment stays disabled.
    """
    global _news_engine
    settings = get_settings()
    analyzer: Optional[SentimentAnalyzer] = None
    if settings.news_sentiment_enabled:
        key = settings.openai_api_key.get_secret_value()
        if key:
            analyzer = SentimentAnalyzer(api_key=key, model=settings.openai_model)
            logger.info("News sentiment ENABLED (model=%s)", settings.openai_model)
        else:
            logger.warning("news_sentiment_enabled=true but OPENAI_API_KEY is empty - sentiment OFF")
    else:
        logger.info("News sentiment disabled")
    _news_engine = NewsEngine(feeds=feeds, analyzer=analyzer)
    logger.info("News Engine initialized")
    return _news_engine


def get_news_engine() -> "NewsEngine":
    """Get the singleton (raises if not initialized)."""
    if _news_engine is None:
        raise NewsEngineNotInitialized("Call init_news_engine() first")
    return _news_engine


def _url_hash(url: str) -> str:
    """Short stable hash of a url for the sentiment cache key."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class NewsEngine:
    """Collects RSS news, optionally annotates sentiment, stores feed in Redis."""

    def __init__(
        self,
        feeds: Dict[str, str] | None = None,
        max_items: int = 60,
        analyzer: Optional[SentimentAnalyzer] = None,
    ) -> None:
        self._feeds = feeds or fetcher.DEFAULT_FEEDS
        self._max_items = max_items
        self._analyzer = analyzer
        logger.info("NewsEngine created (feeds=%d, sentiment=%s)",
                    len(self._feeds), "on" if analyzer else "off")

    async def _apply_sentiment(self, items: List[NewsItem]) -> None:
        """Fill sentiment/sentiment_score on items, using a per-url Redis cache.

        Only items not already in cache are sent to the analyzer (one batched
        request). Fully fault-tolerant: on any error items keep sentiment=None.
        """
        if not self._analyzer or not items:
            return
        redis = get_redis()

        # 1) Try cache for every item
        to_analyze: List[NewsItem] = []
        for it in items:
            try:
                cached = await redis.get(_KEY_SENT_PREFIX + _url_hash(it.url))
            except Exception:  # noqa: BLE001
                cached = None
            if cached:
                try:
                    obj = orjson.loads(cached)
                    it.sentiment = obj.get("sentiment")
                    it.sentiment_score = obj.get("score")
                    continue
                except Exception:  # noqa: BLE001
                    pass
            to_analyze.append(it)

        if not to_analyze:
            logger.info("Sentiment: all %d items from cache", len(items))
            return

        # 2) Analyze the uncached ones (one batched call)
        await self._analyzer.analyze(to_analyze)

        # 3) Write freshly analyzed results back to cache
        cached_n = 0
        for it in to_analyze:
            if it.sentiment is None:
                continue
            try:
                await redis.set(
                    _KEY_SENT_PREFIX + _url_hash(it.url),
                    orjson.dumps({"sentiment": it.sentiment, "score": it.sentiment_score}),
                    ex=_SENT_TTL_SEC,
                )
                cached_n += 1
            except Exception:  # noqa: BLE001
                pass
        logger.info("Sentiment: %d cached, %d analyzed (%d stored)",
                    len(items) - len(to_analyze), len(to_analyze), cached_n)

    async def refresh(self) -> int:
        """Fetch all feeds, annotate sentiment, overwrite feed in Redis."""
        items = await fetcher.fetch_all(self._feeds)
        items = items[: self._max_items]

        # Phase 2: enrich with sentiment (no-op if analyzer is None)
        await self._apply_sentiment(items)

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
