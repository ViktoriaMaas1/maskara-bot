"""
News Fetcher - pure functions to fetch and parse RSS feeds.

parse_rss works on an XML string (no network) - easy to test.
HTTP download is in fetch_feed. RSS parsed with stdlib xml.etree (no feedparser).
"""

from __future__ import annotations

import logging
from email.utils import parsedate_to_datetime
from typing import Dict, List
from xml.etree import ElementTree as ET

import httpx

from .models import NewsItem

logger = logging.getLogger(__name__)

# Free crypto-news RSS sources (no API keys). source-name : url
DEFAULT_FEEDS: Dict[str, str] = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "BitcoinMagazine": "https://bitcoinmagazine.com/feed",
}


def _parse_pubdate(text: str | None) -> int:
    """RFC-822 date from RSS pubDate -> unix ts (sec). 0 on failure."""
    if not text:
        return 0
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return 0
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return 0


def parse_rss(xml_text: str, source: str) -> List[NewsItem]:
    """Parse an RSS-XML string into a list of NewsItem. Pure function."""
    items: List[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("RSS parse error for %s: %s", source, e)
        return items

    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        date_el = item.find("pubDate")
        desc_el = item.find("description")

        title = (title_el.text or "").strip() if title_el is not None else ""
        url = (link_el.text or "").strip() if link_el is not None else ""
        if not title or not url:
            continue

        summary = None
        if desc_el is not None and desc_el.text:
            summary = desc_el.text.strip()
            if len(summary) > 300:
                summary = summary[:297] + "..."

        items.append(
            NewsItem(
                title=title,
                url=url,
                source=source,
                published_ts=_parse_pubdate(date_el.text if date_el is not None else None),
                summary=summary,
            )
        )
    return items


async def fetch_feed(client: httpx.AsyncClient, source: str, url: str) -> List[NewsItem]:
    """Download one RSS feed and parse it. Network errors -> empty list."""
    try:
        resp = await client.get(url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        return parse_rss(resp.text, source)
    except Exception as e:  # noqa: BLE001
        logger.warning("fetch_feed failed for %s (%s): %s", source, url, e)
        return []


async def fetch_all(feeds: Dict[str, str] | None = None) -> List[NewsItem]:
    """Fetch all feeds, merge, dedup by url, sort newest first."""
    feeds = feeds or DEFAULT_FEEDS
    all_items: List[NewsItem] = []

    headers = {"User-Agent": "Mozilla/5.0 (MASKARA-bot RSS reader)"}
    async with httpx.AsyncClient(headers=headers) as client:
        for source, url in feeds.items():
            items = await fetch_feed(client, source, url)
            all_items.extend(items)

    seen = set()
    unique: List[NewsItem] = []
    for it in all_items:
        if it.url in seen:
            continue
        seen.add(it.url)
        unique.append(it)

    unique.sort(key=lambda x: x.published_ts, reverse=True)
    logger.info("fetch_all: %d items from %d feeds", len(unique), len(feeds))
    return unique
