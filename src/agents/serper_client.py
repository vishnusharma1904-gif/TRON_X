"""
Serper.dev Google Search client for TRON-X.
Provides serper_search() — a drop-in replacement for any web search need.

Free tier: 2,500 queries/month.  Get a key at https://serper.dev
Set SERPER_API_KEY in .env to enable; falls back to DuckDuckGo otherwise.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from src.core.logger import log


_SERPER_URL = "https://google.serper.dev/search"
_SERPER_NEWS_URL = "https://google.serper.dev/news"
_SERPER_IMAGES_URL = "https://google.serper.dev/images"


def _get_key() -> str | None:
    try:
        from src.core.config import get_settings
        return get_settings().serper_api_key
    except Exception:
        return None


async def serper_search(
    query: str,
    *,
    num: int = 8,
    gl: str = "in",          # country code (in = India, us = USA, etc.)
    hl: str = "en",           # language
    news: bool = False,
    timeout: float = 10.0,
) -> list[dict]:
    """
    Search via Serper API.  Returns a list of result dicts:
        {title, link, snippet, position}

    Falls back to DuckDuckGo if SERPER_API_KEY is not set.
    """
    key = _get_key()
    if not key:
        log.warning("[serper] No SERPER_API_KEY — falling back to DuckDuckGo")
        return await _ddg_fallback(query, num)

    url = _SERPER_NEWS_URL if news else _SERPER_URL
    headers = {
        "X-API-KEY": key,
        "Content-Type": "application/json",
    }
    payload = {"q": query, "num": num, "gl": gl, "hl": hl}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        results: list[dict] = []

        # Knowledge Graph snippet (quick answer)
        kg = data.get("knowledgeGraph", {})
        if kg.get("description"):
            results.append({
                "title": kg.get("title", "Knowledge Graph"),
                "link": kg.get("website", ""),
                "snippet": kg.get("description", ""),
                "position": 0,
                "source": "knowledge_graph",
            })

        # Answer box
        ab = data.get("answerBox", {})
        if ab.get("answer") or ab.get("snippet"):
            results.append({
                "title": ab.get("title", "Answer"),
                "link": ab.get("link", ""),
                "snippet": ab.get("answer") or ab.get("snippet") or "",
                "position": 0,
                "source": "answer_box",
            })

        # Organic results
        for r in data.get("organic", []):
            results.append({
                "title":    r.get("title", ""),
                "link":     r.get("link", ""),
                "snippet":  r.get("snippet", ""),
                "position": r.get("position", 99),
                "source":   "organic",
            })

        # News results (when news=True)
        for r in data.get("news", []):
            results.append({
                "title":    r.get("title", ""),
                "link":     r.get("link", ""),
                "snippet":  r.get("snippet", ""),
                "date":     r.get("date", ""),
                "position": r.get("position", 99),
                "source":   "news",
            })

        log.info(f"[serper] '{query[:60]}' → {len(results)} results")
        return results[:num]

    except httpx.HTTPStatusError as e:
        log.error(f"[serper] HTTP {e.response.status_code}: {e}")
        return await _ddg_fallback(query, num)
    except Exception as e:
        log.error(f"[serper] search failed: {e}")
        return await _ddg_fallback(query, num)


async def _ddg_fallback(query: str, num: int) -> list[dict]:
    """DuckDuckGo fallback when Serper key is unavailable."""
    try:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            raw = await ddgs.atext(query, max_results=num)
        return [
            {
                "title":   r.get("title", ""),
                "link":    r.get("href", ""),
                "snippet": r.get("body", ""),
                "source":  "ddg",
            }
            for r in raw
        ]
    except Exception as e:
        log.warning(f"[serper] DDG fallback also failed: {e}")
        return []


def format_results_as_text(results: list[dict], max_results: int = 6) -> str:
    """Turn results list into a plain-text block suitable for LLM context."""
    if not results:
        return "No results found."
    lines = []
    for r in results[:max_results]:
        title   = r.get("title", "").strip()
        snippet = r.get("snippet", "").strip()
        link    = r.get("link", "").strip()
        date    = r.get("date", "")
        entry   = f"• {title}"
        if date:
            entry += f" ({date})"
        if snippet:
            entry += f"\n  {snippet}"
        if link:
            entry += f"\n  {link}"
        lines.append(entry)
    return "\n\n".join(lines)
