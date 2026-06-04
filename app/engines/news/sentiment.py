"""
Sentiment analysis for news items via OpenAI (Stage 10, Phase 2).

Takes a batch of NewsItem objects, sends title+summary to gpt-4o-mini,
gets back bullish/neutral/bearish classification + a score in [-1, 1].

Design:
- Batch all items in ONE request (cheap, fast). Model returns a JSON array.
- Fully fault-tolerant: any error (network, bad key, malformed JSON) leaves
  items unannotated (sentiment stays None). The bot never crashes on this.
- Caching is handled by the caller (engine) via Redis, keyed by url, so each
  item is analyzed only once.

Usage:
    analyzer = SentimentAnalyzer(api_key="sk-...", model="gpt-4o-mini")
    annotated = await analyzer.analyze(items)   # returns same list, fields filled
"""

from __future__ import annotations

import logging
from typing import List, Optional

import httpx
import orjson

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_VALID = {"bullish", "neutral", "bearish"}

_SYSTEM_PROMPT = (
    "You are a crypto market analyst. For each news item, classify its likely "
    "short-term impact on crypto asset prices as bullish, neutral, or bearish, "
    "and give a score from -1.0 (very bearish) to 1.0 (very bullish), 0.0 = neutral. "
    "Return one result per item, in the same order, using the item's index."
)

# Structured Outputs schema - guarantees the model returns exactly this shape.
_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "news_sentiment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "idx": {"type": "integer"},
                            "sentiment": {
                                "type": "string",
                                "enum": ["bullish", "neutral", "bearish"],
                            },
                            "score": {"type": "number"},
                        },
                        "required": ["idx", "sentiment", "score"],
                    },
                }
            },
            "required": ["results"],
        },
    },
}


class SentimentAnalyzer:
    """Analyzes news sentiment via the OpenAI chat completions API."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        timeout_sec: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_sec

    def _build_user_prompt(self, items: List[dict]) -> str:
        """Build the user message listing all items with their index."""
        lines = []
        for i, it in enumerate(items):
            title = (it.get("title") or "").strip()
            summary = (it.get("summary") or "").strip()
            # Keep summary short to save tokens
            if len(summary) > 300:
                summary = summary[:300]
            block = f"[{i}] {title}"
            if summary:
                block += f"\n    {summary}"
            lines.append(block)
        return "News items:\n" + "\n".join(lines)

    async def analyze(self, items: List["object"]) -> List["object"]:
        """
        Annotate items in-place with sentiment + sentiment_score.

        `items` is a list of objects with .title/.summary attributes and
        writable .sentiment/.sentiment_score (e.g. NewsItem). Returns the
        same list. On any failure, items are returned unchanged.
        """
        if not items:
            return items

        # Build a plain-dict view for the prompt
        view = [{"title": getattr(it, "title", ""), "summary": getattr(it, "summary", "")} for it in items]
        user_prompt = self._build_user_prompt(view)

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": _RESPONSE_SCHEMA,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    _OPENAI_URL,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code != 200:
                logger.warning(
                    "OpenAI returned non-200",
                    extra={"status": resp.status_code, "body": resp.text[:300]},
                )
                return items

            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            parsed = self._parse_response(content, len(items))
        except Exception as e:  # noqa: BLE001
            logger.exception("Sentiment analysis failed - leaving items unannotated",
                             extra={"error": str(e)})
            return items

        # Apply results
        applied = 0
        for entry in parsed:
            idx = entry.get("idx")
            if not isinstance(idx, int) or idx < 0 or idx >= len(items):
                continue
            sent = entry.get("sentiment")
            score = entry.get("score")
            if sent in _VALID:
                items[idx].sentiment = sent
                try:
                    items[idx].sentiment_score = max(-1.0, min(1.0, float(score)))
                except (TypeError, ValueError):
                    items[idx].sentiment_score = None
                applied += 1

        logger.info("Sentiment analysis done",
                    extra={"requested": len(items), "applied": applied})
        return items

    @staticmethod
    def _parse_response(content: str, n: int) -> List[dict]:
        """Parse the model's JSON response into a list of result dicts."""
        try:
            obj = orjson.loads(content)
        except orjson.JSONDecodeError:
            logger.warning("Could not parse OpenAI JSON response")
            return []
        # Expect {"results": [...]} but tolerate a bare array
        if isinstance(obj, dict):
            results = obj.get("results")
            if results is None:
                # maybe the model used another key - take first list value
                for v in obj.values():
                    if isinstance(v, list):
                        results = v
                        break
        elif isinstance(obj, list):
            results = obj
        else:
            results = None

        if not isinstance(results, list):
            logger.warning("OpenAI response has no result array")
            return []
        return [r for r in results if isinstance(r, dict)]
