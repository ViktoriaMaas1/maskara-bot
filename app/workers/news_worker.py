"""
NewsWorker - background asyncio task that refreshes the news feed.

Every interval_sec seconds: NewsEngine.refresh() then interruptible sleep.
One failed iteration does not kill the loop. Pattern mirrors SignalWorker.

Usage (main.py lifespan):
    worker = init_news_worker(engine, interval_sec=300)
    await worker.start()
    ...
    await close_news_worker()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.engines.news.engine import NewsEngine

logger = logging.getLogger(__name__)


class NewsWorker:
    """Background loop: refresh -> sleep."""

    def __init__(self, engine: NewsEngine, interval_sec: int = 300) -> None:
        if interval_sec <= 0:
            raise ValueError(f"interval_sec must be > 0, got {interval_sec}")

        self._engine = engine
        self._interval = interval_sec
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Start the loop. Idempotent - a second start is a no-op."""
        if self._task is not None and not self._task.done():
            logger.warning("NewsWorker already started")
            return

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="news_worker")
        logger.info("NewsWorker started", extra={"interval_sec": self._interval})

    async def stop(self) -> None:
        """Stop gracefully: set stop_event, wait for task to finish."""
        if self._task is None:
            return

        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._interval + 2)
        except asyncio.TimeoutError:
            logger.warning("NewsWorker did not finish in time - cancelling")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
            logger.info("NewsWorker stopped")

    async def _run(self) -> None:
        """Main loop. Runs until stop_event is set."""
        while not self._stop_event.is_set():
            try:
                await self._engine.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("NewsWorker tick failed - continuing",
                                 extra={"error": str(e)})

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                break
            except asyncio.TimeoutError:
                continue


_news_worker: Optional[NewsWorker] = None


def init_news_worker(engine: NewsEngine, interval_sec: int = 300) -> NewsWorker:
    """Create and store the NewsWorker singleton. Called in lifespan."""
    global _news_worker
    if _news_worker is not None:
        logger.warning("NewsWorker already initialized")
        return _news_worker
    _news_worker = NewsWorker(engine=engine, interval_sec=interval_sec)
    return _news_worker


def get_news_worker() -> NewsWorker:
    """Get the NewsWorker singleton (raises if not initialized)."""
    if _news_worker is None:
        raise RuntimeError("NewsWorker not initialized. Call init_news_worker() in lifespan.")
    return _news_worker


async def close_news_worker() -> None:
    """Stop the singleton gracefully. Called at shutdown."""
    global _news_worker
    if _news_worker is None:
        return
    await _news_worker.stop()
    _news_worker = None
