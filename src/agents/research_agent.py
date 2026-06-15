"""
TRON-X Research Agent
──────────────────────
Web search + summarisation agent.
Uses DuckDuckGo (free, no API key) via duckduckgo-search library,
with httpx fallback scraping for page content.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from src.core.logger import log


class ResearchAgent:
    """Search the web, fetch top pages, summarise findings."""

    MAX_RESULTS   = 5
    MAX_PAGE_CHARS = 4000

    async def run(self, query: str, persona: str = "jarvis",
                  session_id: str = "__research__") -> str:
        log.info(f"[research] Query: {query[:80]}")

        # 1. Search
        snippets = await self._search(query)
        if not snippets:
            return f"No web results found for: {query}"

        # 2. Fetch top page content (best result)
        full_text = await self._fetch_page(snippets[0].get("href", ""))

        # 3. Build context
        search_ctx = "\n\n".join(
            f"[{i+1}] {s.get('title','')}\n{s.get('body','')}\n{s.get('href','')}"
            for i, s in enumerate(snippets)
        )
        if full_text:
            search_ctx += f"\n\n--- TOP RESULT CONTENT ---\n{full_text[:self.MAX_PAGE_CHARS]}"

        # 4. Summarise with LLM
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        prompt = (
            f"Research query: {query}\n\n"
            f"Web search results:\n{search_ctx}\n\n"
            "Provide a comprehensive, factual answer based on the above results. "
            "Cite sources where relevant."
        )
        result = await orch.chat(
            user_message=prompt,
            session_id=session_id,
            intent="research",
            persona=persona,
            max_tokens=1000,
        )
        return result.get("reply", search_ctx[:2000])

    async def _search(self, query: str) -> list[dict]:
        """Search via Serper (preferred) or DuckDuckGo fallback."""
        # Try Serper first — returns normalised {title, link, snippet} dicts
        try:
            from src.agents.serper_client import serper_search, _get_key
            if _get_key():
                raw = await serper_search(query, num=self.MAX_RESULTS)
                # Normalise to the format the rest of ResearchAgent expects
                return [
                    {
                        "title": r.get("title", ""),
                        "href":  r.get("link", ""),
                        "body":  r.get("snippet", ""),
                    }
                    for r in raw
                ]
        except Exception as e:
            log.warning(f"[research] Serper search failed, trying DDG: {e}")

        # Fallback: DuckDuckGo
        try:
            from duckduckgo_search import AsyncDDGS
            async with AsyncDDGS() as ddgs:
                results = await ddgs.atext(query, max_results=self.MAX_RESULTS)
                return list(results)
        except ImportError:
            log.warning("[research] duckduckgo-search not installed, returning empty")
            return []
        except Exception as e:
            log.warning(f"[research] DDG search also failed: {e}")
            return []

    async def _fetch_page(self, url: str) -> str:
        """Fetch and extract text from a URL."""
        if not url:
            return ""
        try:
            import httpx
            from html.parser import HTMLParser

            class _TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.texts: list[str] = []
                    self._skip = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("script", "style", "nav", "footer", "header"):
                        self._skip = True

                def handle_endtag(self, tag):
                    if tag in ("script", "style", "nav", "footer", "header"):
                        self._skip = False

                def handle_data(self, data):
                    if not self._skip:
                        stripped = data.strip()
                        if stripped:
                            self.texts.append(stripped)

            async with httpx.AsyncClient(
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (compatible; TRON-X/1.0)"},
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                parser = _TextExtractor()
                parser.feed(resp.text)
                return " ".join(parser.texts)

        except Exception as e:
            log.debug(f"[research] Page fetch failed ({url}): {e}")
            return ""


# =============================================================================
# Phase 9 additions -- ResearchAgentV2 with provider cascade + SSE streaming
# =============================================================================
import json
import os
import re
import time
from typing import AsyncGenerator
import httpx


class ResearchAgentV2:
    """Multi-provider web research agent with Perplexity fast-path.
    Cascade: Perplexity -> Brave -> Serper -> DuckDuckGo.
    """
    MAX_SEARCH_RESULTS = 5
    MAX_PAGE_CHARS     = 3000
    MAX_HOPS           = 2

    def __init__(self) -> None:
        self._last_provider: str = "unknown"

    # ------------------------------------------------------------------
    # Search providers
    # ------------------------------------------------------------------
    async def _search_brave(self, query: str) -> list[dict]:
        try:
            api_key = os.getenv("BRAVE_API_KEY", "")
            if not api_key:
                return []
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": 5},
                    headers={"X-Subscription-Token": api_key},
                )
                resp.raise_for_status()
                data = resp.json()
            results = data.get("web", {}).get("results", [])
            return [{"title": r.get("title",""), "url": r.get("url",""), "snippet": r.get("description","")} for r in results]
        except Exception as e:
            log.warning(f"[ResearchAgentV2] Brave search failed: {e}")
            return []

    async def _search_serper(self, query: str) -> list[dict]:
        try:
            api_key = os.getenv("SERPER_API_KEY", "")
            if not api_key:
                return []
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "num": 5},
                    headers={"X-API-KEY": api_key},
                )
                resp.raise_for_status()
                data = resp.json()
            results = data.get("organic", [])
            return [{"title": r.get("title",""), "url": r.get("link",""), "snippet": r.get("snippet","")} for r in results]
        except Exception as e:
            log.warning(f"[ResearchAgentV2] Serper search failed: {e}")
            return []

    async def _search_ddg(self, query: str) -> list[dict]:
        try:
            from duckduckgo_search import AsyncDDGS
        except ImportError:
            log.warning("[ResearchAgentV2] duckduckgo_search not installed")
            return []
        try:
            async with AsyncDDGS() as ddgs:
                results = await ddgs.atext(query, max_results=5)
            return [{"title": r.get("title",""), "url": r.get("href",""), "snippet": r.get("body","")} for r in results]
        except Exception as e:
            log.warning(f"[ResearchAgentV2] DDG search failed: {e}")
            return []

    async def _search(self, query: str) -> list[dict]:
        if os.getenv("PERPLEXITYAI_API_KEY"):
            return []
        if os.getenv("BRAVE_API_KEY"):
            results = await self._search_brave(query)
            if results:
                self._last_provider = "brave"
                return results
        if os.getenv("SERPER_API_KEY"):
            results = await self._search_serper(query)
            if results:
                self._last_provider = "serper"
                return results
        results = await self._search_ddg(query)
        self._last_provider = "ddg"
        return results

    # ------------------------------------------------------------------
    # Page fetching
    # ------------------------------------------------------------------
    async def _fetch_one(self, url: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                response = await client.get(url)
                html = response.text
            html = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text).strip()
            return {"url": url, "content": text[:self.MAX_PAGE_CHARS]}
        except Exception as e:
            log.warning(f"[ResearchAgentV2] fetch failed for {url}: {e}")
            return {"url": url, "content": ""}

    async def _fetch_pages(self, urls: list[str]) -> list[dict]:
        return await asyncio.gather(*[self._fetch_one(u) for u in urls[:3]])

    # ------------------------------------------------------------------
    # Perplexity fast path
    # ------------------------------------------------------------------
    async def _run_perplexity(self, query: str) -> dict:
        try:
            from src.intelligence.router import get_router
            router = get_router()
            response, model = await router.complete(
                messages=[
                    {"role": "system", "content": "You are a research assistant. Always cite your sources."},
                    {"role": "user", "content": query},
                ],
                category="research",
                preferred_model="perplexity/sonar-pro",
            )
            reply = response.choices[0].message.content
            return {"answer": reply, "citations": [], "model": model, "provider": "perplexity", "hops": 0}
        except Exception as e:
            log.error(f"[ResearchAgentV2] Perplexity call failed: {e}")
            return {"answer": f"Perplexity research failed: {e}", "citations": [], "model": "unknown", "provider": "perplexity", "hops": 0}

    # ------------------------------------------------------------------
    # Core research
    # ------------------------------------------------------------------
    async def run(self, query: str, max_hops: int = 1) -> dict:
        if os.getenv("PERPLEXITYAI_API_KEY"):
            return await self._run_perplexity(query)

        results = await self._search(query)
        if not results:
            return {"answer": f"No results found for: {query}", "citations": [], "hops": 0}

        urls = [r["url"] for r in results if r.get("url")]
        pages = await self._fetch_pages(urls)

        citations: list[dict] = []
        context_parts: list[str] = []
        for i, (res, page) in enumerate(zip(results[:3], pages)):
            idx = i + 1
            citations.append({"index": idx, "title": res.get("title",""), "url": res.get("url",""), "snippet": res.get("snippet","")})
            context_parts.append(f"[{idx}] {res.get('title','')}\nURL: {res.get('url','')}\nSnippet: {res.get('snippet','')}\nContent: {page.get('content','')}\n")

        context = "\n---\n".join(context_parts)
        hops_used = 1

        if max_hops >= 2:
            try:
                from src.intelligence.router import get_router
                router = get_router()
                fu_resp, _ = await router.complete(
                    messages=[{"role": "user", "content": f"Based on this research context, what ONE follow-up search query would fill the biggest knowledge gap for answering: '{query}'?\n\nContext:\n{context}\n\nRespond with ONLY the query string."}],
                    category="fast_chat",
                )
                followup_query = fu_resp.choices[0].message.content.strip()
                if followup_query:
                    fu_results = await self._search(followup_query)
                    if fu_results:
                        fu_pages = await self._fetch_pages([r["url"] for r in fu_results[:2] if r.get("url")])
                        for j, (fr, fp) in enumerate(zip(fu_results[:2], fu_pages)):
                            idx = 4 + j
                            citations.append({"index": idx, "title": fr.get("title",""), "url": fr.get("url",""), "snippet": fr.get("snippet","")})
                            context_parts.append(f"[{idx}] {fr.get('title','')}\nURL: {fr.get('url','')}\nSnippet: {fr.get('snippet','')}\nContent: {fp.get('content','')}\n")
                        context = "\n---\n".join(context_parts)
                        hops_used = 2
            except Exception as e:
                log.warning(f"[ResearchAgentV2] Second hop failed: {e}")

        try:
            from src.intelligence.router import get_router
            router = get_router()
            response, model = await router.complete(
                messages=[{"role": "user", "content": f"Research question: {query}\n\nSources:\n{context}\n\nWrite a comprehensive, factual answer. Reference sources inline using [1], [2], etc."}],
                category="research",
            )
            answer = response.choices[0].message.content
        except Exception as e:
            log.error(f"[ResearchAgentV2] Synthesis failed: {e}")
            return {"answer": f"Failed to synthesise answer: {e}", "citations": citations, "hops": hops_used, "model": "unknown", "provider": self._last_provider}

        return {"answer": answer, "citations": citations, "hops": hops_used, "model": model, "provider": self._last_provider}

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------
    async def stream(self, query: str, max_hops: int = 1) -> AsyncGenerator[str, None]:
        def _event(payload: dict) -> str:
            return f"data: {json.dumps(payload)}\n\n"

        try:
            if os.getenv("PERPLEXITYAI_API_KEY"):
                yield _event({"type": "progress", "step": "perplexity", "message": "Querying Perplexity sonar-pro..."})
                result = await self._run_perplexity(query)
                yield _event({"type": "result", "data": result})
                return

            yield _event({"type": "progress", "step": "search", "message": f"Searching for: {query}..."})
            results = await self._search(query)
            if not results:
                yield _event({"type": "result", "data": {"answer": f"No results found for: {query}", "citations": [], "hops": 0}})
                return

            yield _event({"type": "progress", "step": "fetch", "message": f"Reading {len(results)} sources..."})
            urls = [r["url"] for r in results if r.get("url")]
            pages = []
            for url in urls[:3]:
                yield _event({"type": "progress", "step": "reading", "message": f"Reading: {url[:60]}..."})
                page = await self._fetch_one(url)
                pages.append(page)

            citations: list[dict] = []
            context_parts: list[str] = []
            for i, (res, page) in enumerate(zip(results[:3], pages)):
                idx = i + 1
                citations.append({"index": idx, "title": res.get("title",""), "url": res.get("url",""), "snippet": res.get("snippet","")})
                context_parts.append(f"[{idx}] {res.get('title','')}\nURL: {res.get('url','')}\nSnippet: {res.get('snippet','')}\nContent: {page.get('content','')}\n")

            context = "\n---\n".join(context_parts)
            hops_used = 1

            if max_hops >= 2:
                try:
                    from src.intelligence.router import get_router
                    router = get_router()
                    fu_resp, _ = await router.complete(
                        messages=[{"role": "user", "content": f"Based on this research context, what ONE follow-up search query would fill the biggest knowledge gap for answering: '{query}'?\n\nContext:\n{context}\n\nRespond with ONLY the query string."}],
                        category="fast_chat",
                    )
                    followup_query = fu_resp.choices[0].message.content.strip()
                    if followup_query:
                        yield _event({"type": "progress", "step": "deep_search", "message": f"Going deeper: {followup_query}..."})
                        fu_results = await self._search(followup_query)
                        if fu_results:
                            fu_urls = [r["url"] for r in fu_results[:2] if r.get("url")]
                            for url in fu_urls:
                                yield _event({"type": "progress", "step": "reading", "message": f"Reading: {url[:60]}..."})
                            fu_pages = [await self._fetch_one(u) for u in fu_urls]
                            for j, (fr, fp) in enumerate(zip(fu_results[:2], fu_pages)):
                                idx = 4 + j
                                citations.append({"index": idx, "title": fr.get("title",""), "url": fr.get("url",""), "snippet": fr.get("snippet","")})
                                context_parts.append(f"[{idx}] {fr.get('title','')}\nURL: {fr.get('url','')}\nSnippet: {fr.get('snippet','')}\nContent: {fp.get('content','')}\n")
                            context = "\n---\n".join(context_parts)
                            hops_used = 2
                except Exception as e:
                    log.warning(f"[ResearchAgentV2] Stream second hop failed: {e}")

            yield _event({"type": "progress", "step": "synthesise", "message": "Synthesising answer..."})
            try:
                from src.intelligence.router import get_router
                router = get_router()
                response, model = await router.complete(
                    messages=[{"role": "user", "content": f"Research question: {query}\n\nSources:\n{context}\n\nWrite a comprehensive, factual answer. Reference sources inline using [1], [2], etc."}],
                    category="research",
                )
                answer = response.choices[0].message.content
            except Exception as e:
                log.error(f"[ResearchAgentV2] Stream synthesis failed: {e}")
                yield _event({"type": "error", "message": str(e)})
                return

            yield _event({"type": "result", "data": {"answer": answer, "citations": citations, "hops": hops_used, "model": model, "provider": self._last_provider}})

        except Exception as e:
            log.error(f"[ResearchAgentV2] Stream error: {e}")
            yield _event({"type": "error", "message": str(e)})
