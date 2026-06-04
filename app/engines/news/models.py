"""News models - Pydantic schemas for the news feed (Stage 10, Phase 1)."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class NewsItem(BaseModel):
    """A single news item from an RSS feed."""

    title: str
    url: str
    source: str
    published_ts: int
    summary: Optional[str] = None
    sentiment: Optional[str] = None
    sentiment_score: Optional[float] = None


class NewsSnapshot(BaseModel):
    """Snapshot of the news feed served to the dashboard."""

    data_available: bool = False
    count: int = 0
    items: List[NewsItem] = []
    last_fetch_ts: int = 0
