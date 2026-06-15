"""
TRON-X News Feed  --  Phase 15

Providers (in order):
  1. NewsAPI.org     -- top headlines + search (requires NEWSAPI_KEY)
  2. Google News RSS -- free, no key, feedparser-based fallback

TTL cache: 15 minutes
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from src.core.config import get_settings
from src.core.logger import log

settings   = get_settings()
CACHE_TTL  = 900    # 15 minutes
NEWSAPI_BASE = "https://newsapi.org/v2"

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str) -> Any | None:
    if key in _cache:
        ts, val = _cache[key]
        if time.monotonic() - ts < CACHE_TTL:
            return val
    return None


def _store(key: str, val: Any) -> None:
    _cache[key] = (time.monotonic(), val)


async def _http_get(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    try:
        import aiohttp
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "aiohttp",
             "--break-system-packages", "--quiet"], check=True
        )
        import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "json" in ct:
                return await r.json()
            return await r.text()


def _ensure_feedparser():
    try:
        import feedparser
        return feedparser
    except ImportError:
        import subprocess, sys
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "feedparser",
             "--break-system-packages", "--quiet"], check=True
        )
        import feedparser
        return feedparser


# ---------------------------------------------------------------------------
# NewsFeed
# ---------------------------------------------------------------------------

class NewsFeed:
    """News headlines and search via NewsAPI + Google News RSS fallback."""

    VALID_CATEGORIES = {
        "business", "entertainment", "general", "health",
        "science", "sports", "technology",
    }

    async def headlines(
        self,
        topic:   Optional[str] = None,
        country: str = "us",
        count:   int = 10,
    ) -> dict:
        """
        Top headlines, optionally filtered by topic/category.

        Returns:
            {
              "articles": [
                {
                  "title":       str,
                  "description": str,
                  "url":         str,
                  "source":      str,
                  "published":   str,   # ISO 8601
                  "image":       str | None,
                }
              ],
              "total_results": int,
              "query":         str,
              "provider":      str,
            }
        """
        count = max(1, min(count, 100))
        key   = f"headlines:{topic}:{country}:{count}"
        if (cached := _cached(key)):
            return cached

        result = None
        if settings.newsapi_key:
            result = await self._newsapi_headlines(topic, country, count)
        if result is None:
            result = await self._rss_headlines(topic, count)

        if result:
            _store(key, result)
        return result or {"articles": [], "total_results": 0, "query": topic or "top", "provider": "none"}

    async def search(self, query: str, count: int = 10) -> dict:
        """
        Full-text news search across all sources.

        Returns same structure as headlines().
        """
        count = max(1, min(count, 100))
        key   = f"search:{query.lower()}:{count}"
        if (cached := _cached(key)):
            return cached

        result = None
        if settings.newsapi_key:
            result = await self._newsapi_search(query, count)
        if result is None:
            result = await self._rss_search(query, count)

        if result:
            _store(key, result)
        return result or {"articles": [], "total_results": 0, "query": query, "provider": "none"}

    # -- NewsAPI internals ---------------------------------------------------

    def _article_from_newsapi(self, a: dict) -> dict:
        return {
            "title":       a.get("title", ""),
            "description": a.get("description", ""),
            "url":         a.get("url", ""),
            "source":      (a.get("source") or {}).get("name", ""),
            "published":   a.get("publishedAt", ""),
            "image":       a.get("urlToImage"),
        }

    async def _newsapi_headlines(
        self, topic: Optional[str], country: str, count: int
    ) -> dict | None:
        try:
            params: dict = {
                "country":  country,
                "pageSize": count,
                "apiKey":   settings.newsapi_key,
            }
            if topic:
                if topic.lower() in self.VALID_CATEGORIES:
                    params["category"] = topic.lower()
                else:
                    params["q"] = topic

            data = await _http_get(f"{NEWSAPI_BASE}/top-headlines", params)
            if data.get("status") != "ok":
                return None

            return {
                "articles":      [self._article_from_newsapi(a) for a in data.get("articles", [])],
                "total_results": data.get("totalResults", 0),
                "query":         topic or "top",
                "provider":      "newsapi",
            }
        except Exception as e:
            log.warning("[news] NewsAPI headlines failed: %s", e)
            return None

    async def _newsapi_search(self, query: str, count: int) -> dict | None:
        try:
            params = {
                "q":        query,
                "pageSize": count,
                "sortBy":   "publishedAt",
                "apiKey":   settings.newsapi_key,
            }
            data = await _http_get(f"{NEWSAPI_BASE}/everything", params)
            if data.get("status") != "ok":
                return None

            return {
                "articles":      [self._article_from_newsapi(a) for a in data.get("articles", [])],
                "total_results": data.get("totalResults", 0),
                "query":         query,
                "provider":      "newsapi",
            }
        except Exception as e:
            log.warning("[news] NewsAPI search failed: %s", e)
            return None

    # -- RSS / Google News fallback ------------------------------------------

    def _article_from_rss(self, entry: Any) -> dict:
        return {
            "title":       getattr(entry, "title", ""),
            "description": getattr(entry, "summary", ""),
            "url":         getattr(entry, "link", ""),
            "source":      getattr(getattr(entry, "source", None), "title", ""),
            "published":   getattr(entry, "published", ""),
            "image":       None,
        }

    async def _rss_fetch(self, rss_url: str, count: int) -> list[dict]:
        loop = asyncio.get_event_loop()

        def _run():
            fp = _ensure_feedparser()
            feed = fp.parse(rss_url)
            return [self._article_from_rss(e) for e in feed.entries[:count]]

        try:
            return await loop.run_in_executor(None, _run)
        except Exception as e:
            log.warning("[news] RSS fetch failed: %s", e)
            return []

    async def _rss_headlines(self, topic: Optional[str], count: int) -> dict | None:
        try:
            if topic:
                import urllib.parse
                rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(topic)}&hl=en-US&gl=US&ceid=US:en"
            else:
                rss_url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"

            articles = await self._rss_fetch(rss_url, count)
            if not articles:
                return None
            return {
                "articles":      articles,
                "total_results": len(articles),
                "query":         topic or "top",
                "provider":      "google_news_rss",
            }
        except Exception as e:
            log.warning("[news] RSS headlines failed: %s", e)
            return None

    async def _rss_search(self, query: str, count: int) -> dict | None:
        try:
            import urllib.parse
            rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en-US&gl=US&ceid=US:en"
            articles = await self._rss_fetch(rss_url, count)
            if not articles:
                return None
            return {
                "articles":      articles,
                "total_results": len(articles),
                "query":         query,
                "provider":      "google_news_rss",
            }
        except Exception as e:
            log.warning("[news] RSS search failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_feed: NewsFeed | None = None

def get_news_feed() -> NewsFeed:
    global _feed
    if _feed is None:
        _feed = NewsFeed()
    return _feed
