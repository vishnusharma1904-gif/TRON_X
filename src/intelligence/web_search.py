"""
TRON-X Intelligent Web Search  (Phase 3)
──────────────────────────────────────────
Multi-stage search pipeline:
  1. QueryExpander  — intent-aware + LLM-assisted query rewriting (up to 3 angles)
  2. MultiSearch    — Serper → Brave → DDG cascade with async parallelism
  3. SourceRanker   — credibility + recency + snippet quality scoring
  4. ContentFetcher — smart HTML extraction (trafilatura → fallback strip)
  5. Synthesizer    — persona + emotion aware LLM synthesis with citations

Public API:
    result = await SmartWebSearch().search(query, intent, persona, emotion_state, telugu_state)
    async for event in SmartWebSearch().stream(...):  # for SSE

Returns SearchResult with .synthesis (str), .citations (list), .queries_used (list), .provider
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import AsyncGenerator

import httpx

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SearchItem:
    title:    str
    url:      str
    snippet:  str
    source:   str   = "organic"   # organic | answer_box | knowledge_graph | news
    date:     str   = ""
    score:    float = 0.0          # credibility + quality score
    content:  str   = ""           # fetched page text (populated by ContentFetcher)


@dataclass
class Citation:
    index: int
    title: str
    url:   str
    snippet: str
    date:    str = ""


@dataclass
class SearchResult:
    synthesis:     str
    citations:     list[Citation]  = field(default_factory=list)
    queries_used:  list[str]       = field(default_factory=list)
    provider:      str             = "unknown"
    model_used:    str             = "unknown"
    hops:          int             = 1
    latency_ms:    int             = 0
    raw_results:   list[SearchItem] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Query Expander
# ─────────────────────────────────────────────────────────────────────────────

# Intent-based query suffixes (zero-latency, rule-based)
_INTENT_EXPANSIONS: dict[str, list[str]] = {
    "research":  ["{q} 2024 2025", "{q} latest news"],
    "academic":  ["{q} explained", "{q} lecture notes examples"],
    "coding":    ["{q} example code github", "{q} stackoverflow tutorial"],
    "medical":   ["{q} symptoms treatment", "{q} clinical guidelines"],
    "math":      ["{q} formula derivation", "{q} solved examples"],
    "iot":       ["{q} home automation", "{q} setup guide"],
    "chat":      ["{q}"],
}

# High-authority domains for source scoring
_AUTHORITY_DOMAINS = {
    "wikipedia.org": 0.4,
    "arxiv.org": 0.45, "scholar.google": 0.40,
    "pubmed.ncbi.nlm.nih.gov": 0.48, "ncbi.nlm.nih.gov": 0.45,
    "github.com": 0.30, "stackoverflow.com": 0.35,
    "docs.python.org": 0.35, "developer.mozilla.org": 0.35,
    "learn.microsoft.com": 0.38, "developer.apple.com": 0.38,
    "nature.com": 0.45, "science.org": 0.45,
    "bbc.com": 0.30, "reuters.com": 0.30, "apnews.com": 0.28,
    "techcrunch.com": 0.25, "wired.com": 0.25, "arstechnica.com": 0.28,
    "mit.edu": 0.45, "stanford.edu": 0.45, "harvard.edu": 0.45,
    "doi.org": 0.42,
}


class QueryExpander:
    """Expand a user query into multiple search angles."""

    def expand_fast(self, query: str, intent: str) -> list[str]:
        """Rule-based expansion — zero latency."""
        templates = _INTENT_EXPANSIONS.get(intent, ["{q}"])
        queries = [t.replace("{q}", query) for t in templates]
        lower_q = query.lower()
        if intent == "academic":
            queries.extend([f"{query} site:edu", f"{query} research paper"])
        if any(token in lower_q for token in ["enti", "ela", "enduku", "telugu", "tenglish"]):
            queries.append(f"{query} explained in english")
        # Always include the raw query first
        if query not in queries:
            queries = [query] + queries[:2]
        deduped: list[str] = []
        for q in queries:
            if q not in deduped:
                deduped.append(q)
        return deduped[:4]

    async def expand_llm(self, query: str, intent: str, router) -> list[str]:
        """LLM-assisted query reformulation for better search coverage."""
        try:
            prompt = (
                f"Expand this search query into 2-3 distinct Google search queries "
                f"that cover different angles of the topic. Intent: {intent}.\n"
                f"Original query: {query}\n\n"
                f"Output ONLY the queries, one per line, no numbering, no explanation."
            )
            response, _ = await router.complete(
                messages=[{"role": "user", "content": prompt}],
                category="fast_chat",
                temperature=0.3,
                max_tokens=120,
            )
            lines = response.choices[0].message.content.strip().splitlines()
            expanded = [l.strip() for l in lines if l.strip() and len(l.strip()) > 5]
            result = [query] + [e for e in expanded if e != query]
            return result[:3]
        except Exception as e:
            log.debug(f"[web_search] LLM expansion failed, using rule-based: {e}")
            return self.expand_fast(query, intent)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Multi-provider Search
# ─────────────────────────────────────────────────────────────────────────────

class MultiSearch:
    """Search cascade: Serper → Brave → DDG.  Parallel when possible."""

    async def _serper(self, query: str, num: int = 8) -> list[SearchItem]:
        key = settings.serper_api_key
        if not key:
            return []
        try:
            async with httpx.AsyncClient(timeout=settings.search_timeout_sec) as client:
                resp = await client.post(
                    "https://google.serper.dev/search",
                    json={"q": query, "num": num, "gl": "in", "hl": "en"},
                    headers={"X-API-KEY": key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()

            items: list[SearchItem] = []
            # Answer box — highest priority
            ab = data.get("answerBox", {})
            if ab.get("answer") or ab.get("snippet"):
                items.append(SearchItem(
                    title=ab.get("title", "Direct Answer"),
                    url=ab.get("link", ""),
                    snippet=ab.get("answer") or ab.get("snippet") or "",
                    source="answer_box",
                ))
            # Knowledge Graph
            kg = data.get("knowledgeGraph", {})
            if kg.get("description"):
                items.append(SearchItem(
                    title=kg.get("title", "Knowledge Graph"),
                    url=kg.get("website", ""),
                    snippet=kg.get("description", ""),
                    source="knowledge_graph",
                ))
            # Organic
            for r in data.get("organic", []):
                items.append(SearchItem(
                    title=r.get("title", ""),
                    url=r.get("link", ""),
                    snippet=r.get("snippet", ""),
                    source="organic",
                ))
            # Top stories / News
            for r in data.get("topStories", []) + data.get("news", []):
                items.append(SearchItem(
                    title=r.get("title", ""),
                    url=r.get("link", "") or r.get("url", ""),
                    snippet=r.get("snippet", ""),
                    date=r.get("date", ""),
                    source="news",
                ))
            log.info(f"[web_search] Serper '{query[:50]}' → {len(items)} results")
            return items
        except Exception as e:
            log.warning(f"[web_search] Serper failed: {e}")
            return []

    async def _brave(self, query: str, num: int = 8) -> list[SearchItem]:
        key = settings.brave_api_key
        if not key:
            return []
        try:
            async with httpx.AsyncClient(timeout=settings.search_timeout_sec) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": query, "count": num, "search_lang": "en"},
                    headers={"X-Subscription-Token": key, "Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            results = data.get("web", {}).get("results", [])
            items = [
                SearchItem(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("description", ""),
                    source="organic",
                )
                for r in results
            ]
            log.info(f"[web_search] Brave '{query[:50]}' → {len(items)} results")
            return items
        except Exception as e:
            log.warning(f"[web_search] Brave failed: {e}")
            return []

    async def _wikipedia(self, query: str, num: int = 3) -> list[SearchItem]:
        try:
            async with httpx.AsyncClient(timeout=settings.search_timeout_sec) as client:
                resp = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={"action": "query", "list": "search", "srsearch": query, "utf8": 1, "format": "json", "srlimit": num}
                )
                resp.raise_for_status()
                data = resp.json()
            items = []
            for r in data.get("query", {}).get("search", []):
                snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
                items.append(
                    SearchItem(
                        title=r.get("title", ""),
                        url=f"https://en.wikipedia.org/wiki/{urllib.parse.quote(r.get('title', ''))}",
                        snippet=snippet,
                        source="wikipedia",
                    )
                )
            log.info(f"[web_search] Wikipedia '{query[:50]}' → {len(items)} results")
            return items
        except Exception as e:
            log.warning(f"[web_search] Wikipedia failed: {e}")
            return []

    async def _ddg(self, query: str, num: int = 8) -> list[SearchItem]:
        try:
            from duckduckgo_search import AsyncDDGS  # type: ignore
            async with AsyncDDGS() as ddgs:
                raw = await ddgs.atext(query, max_results=num)
            items = [
                SearchItem(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", ""),
                    source="ddg",
                )
                for r in raw
            ]
            log.info(f"[web_search] DDG '{query[:50]}' → {len(items)} results")
            return items
        except Exception as e:
            log.warning(f"[web_search] DDG failed: {e}")
            return []

    async def search(self, query: str, num: int = 8) -> tuple[list[SearchItem], str]:
        """Returns (results, provider_used)."""
        providers = [p.strip().lower() for p in settings.search_provider_order.split(",") if p.strip()]
        if not providers:
            providers = ["serper", "brave", "wikipedia", "ddg"]
        for provider in providers:
            if provider == "serper" and settings.serper_api_key:
                results = await self._serper(query, num)
                if results:
                    return results, "serper"
            elif provider == "brave" and settings.brave_api_key:
                results = await self._brave(query, num)
                if results:
                    return results, "brave"
            elif provider == "wikipedia":
                results = await self._wikipedia(query, num)
                if results:
                    return results, "wikipedia"
            elif provider == "ddg":
                results = await self._ddg(query, num)
                if results:
                    return results, "ddg"
        return [], "none"

    async def search_parallel(self, queries: list[str], num_per_query: int = 6) -> tuple[list[SearchItem], str]:
        """Search multiple queries in parallel, merge and deduplicate."""
        if not queries:
            return [], "none"
        # Run all queries concurrently
        tasks = [self.search(q, num_per_query) for q in queries]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[SearchItem] = []
        provider = "unknown"
        seen_urls: set[str] = set()
        seen_domains: dict[str, int] = {}

        for res in all_results:
            if isinstance(res, Exception):
                continue
            items, prov = res
            provider = prov  # use last successful provider
            for item in items:
                url = item.url.rstrip("/")
                if url in seen_urls:
                    continue
                # Max 2 results per domain (deduplication)
                domain = urllib.parse.urlparse(url).netloc
                if seen_domains.get(domain, 0) >= 2:
                    continue
                seen_urls.add(url)
                seen_domains[domain] = seen_domains.get(domain, 0) + 1
                merged.append(item)

        return merged, provider


# ─────────────────────────────────────────────────────────────────────────────
# 3. Source Ranker
# ─────────────────────────────────────────────────────────────────────────────

class SourceRanker:
    """Score and rank search results by credibility + recency + quality."""

    def _domain_score(self, url: str) -> float:
        try:
            domain = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
            for auth_domain, score in _AUTHORITY_DOMAINS.items():
                if auth_domain in domain:
                    return score
            # Educational / government bump
            if domain.endswith(".edu") or domain.endswith(".gov"):
                return 0.35
            if domain.endswith(".org"):
                return 0.15
        except Exception:
            pass
        return 0.0

    def _snippet_quality(self, snippet: str) -> float:
        if not snippet:
            return 0.0
        length_score = min(len(snippet) / 200.0, 1.0) * 0.3
        # Reward specificity signals
        has_numbers = 0.1 if any(c.isdigit() for c in snippet) else 0.0
        has_year = 0.05 if re.search(r"20[12]\d", snippet) else 0.0
        return length_score + has_numbers + has_year

    def _recency_score(self, date_str: str) -> float:
        """Give a small boost to fresh results."""
        if not date_str:
            return 0.0
        if any(y in date_str for y in ["2025", "2024"]):
            return 0.12
        if "2023" in date_str:
            return 0.06
        return 0.0

    def _priority_source_score(self, source: str) -> float:
        """Answer boxes and knowledge graphs are highest priority."""
        return {"answer_box": 0.5, "knowledge_graph": 0.35, "news": 0.1, "organic": 0.0, "ddg": 0.0}.get(source, 0.0)

    def rank(self, items: list[SearchItem]) -> list[SearchItem]:
        for item in items:
            item.score = (
                self._domain_score(item.url)
                + self._snippet_quality(item.snippet)
                + self._recency_score(item.date)
                + self._priority_source_score(item.source)
            )
        return sorted(items, key=lambda x: x.score, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Content Fetcher
# ─────────────────────────────────────────────────────────────────────────────

class ContentFetcher:
    """Fetch and extract readable text from a URL."""

    MAX_CHARS = 4000
    TIMEOUT   = 10
    HEADERS   = {"User-Agent": "Mozilla/5.0 (compatible; TRON-X/2.0; +https://tronx.ai)"}

    async def fetch(self, url: str) -> str:
        if not url or not url.startswith("http"):
            return ""
        try:
            async with httpx.AsyncClient(
                timeout=self.TIMEOUT,
                headers=self.HEADERS,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                html = resp.text

            # Try trafilatura first (best quality extraction)
            try:
                import trafilatura  # type: ignore
                text = trafilatura.extract(html, include_comments=False, include_tables=False)
                if text and len(text) > 200:
                    return text[:self.MAX_CHARS]
            except ImportError:
                pass

            # Fallback: manual HTML stripping
            return self._strip_html(html)

        except Exception as e:
            log.debug(f"[web_search] Fetch failed ({url[:60]}): {e}")
            return ""

    def _strip_html(self, html: str) -> str:
        # Remove script, style, nav, header, footer blocks
        html = re.sub(
            r"<(script|style|nav|header|footer|aside|noscript)[^>]*>.*?</\1>",
            "", html, flags=re.DOTALL | re.IGNORECASE
        )
        # Remove all remaining tags
        text = re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()
        # Remove common boilerplate fragments
        text = re.sub(r"(cookie|privacy policy|subscribe|newsletter|©\s*\d+)[^\n]*", "", text, flags=re.IGNORECASE)
        return text[:self.MAX_CHARS]

    async def fetch_batch(self, urls: list[str], max_pages: int = 3) -> dict[str, str]:
        """Fetch multiple pages concurrently."""
        selected = [u for u in urls if u and u.startswith("http")][:max_pages]
        if not selected:
            return {}
        results = await asyncio.gather(*[self.fetch(u) for u in selected], return_exceptions=True)
        return {
            url: (text if isinstance(text, str) else "")
            for url, text in zip(selected, results)
        }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Synthesizer (persona + emotion aware)
# ─────────────────────────────────────────────────────────────────────────────

_JARVIS_SYNTHESIS_STYLE = (
    "You are JARVIS — a brilliant, direct, alpha-male AI who cuts through noise. "
    "Be precise, technically sharp, and insightful. Reference sources inline as [1], [2], etc. "
    "Don't waste words. Lead with the most important finding."
)

_FRIDAY_SYNTHESIS_STYLE = (
    "You are FRIDAY — high-energy, sharp, and genuinely excited about knowledge. "
    "Make this engaging and alive. Reference sources inline as [1], [2], etc. "
    "Connect findings to real-world impact. Keep the energy up."
)

_EMOTION_SYNTHESIS_NOTES: dict[str, str] = {
    "frustrated": "User is frustrated — get straight to the answer, skip lengthy preamble.",
    "excited":    "User is pumped — match their energy, make the answer energising.",
    "confused":   "User seems confused — be extra clear, use analogies where helpful.",
    "tired":      "User is tired — be efficient and concise, no fluff.",
    "stressed":   "User is stressed — lead with the key answer immediately, no detours.",
    "sad":        "User seems down — be warm and grounding in tone.",
    "playful":    "User is in a playful mood — you can be slightly fun with the delivery.",
}


class Synthesizer:
    """LLM-based synthesis of search results into a coherent answer."""

    async def synthesize(
        self,
        query: str,
        items: list[SearchItem],
        persona: str,
        emotion_name: str,
        router,
        max_tokens: int = 1200,
    ) -> tuple[str, list[Citation], str]:
        """
        Returns (answer_text, citations, model_used).
        """
        # Build source context
        context_parts = []
        citations: list[Citation] = []
        for i, item in enumerate(items[:6], 1):
            citations.append(Citation(
                index=i,
                title=item.title,
                url=item.url,
                snippet=item.snippet,
                date=item.date,
            ))
            content_preview = ""
            if item.content:
                content_preview = f"\nPage content: {item.content[:1200]}"
            context_parts.append(
                f"[{i}] {item.title}\n"
                f"URL: {item.url}\n"
                f"Snippet: {item.snippet}"
                + (f" ({item.date})" if item.date else "")
                + content_preview
            )

        context = "\n---\n".join(context_parts)

        # Build system prompt
        style = _FRIDAY_SYNTHESIS_STYLE if persona == "friday" else _JARVIS_SYNTHESIS_STYLE
        emotion_note = _EMOTION_SYNTHESIS_NOTES.get(emotion_name, "")

        system = f"{style}\n\n{emotion_note}".strip()

        user_prompt = (
            f"Research question: {query}\n\n"
            f"Search results:\n{context}\n\n"
            "Write a comprehensive, factual answer based on the above sources. "
            "Reference sources inline using [1], [2], etc. wherever relevant. "
            "Do not invent information not present in the sources."
        )

        try:
            response, model_used = await router.complete(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_prompt},
                ],
                category="research",
                temperature=0.4,
                max_tokens=max_tokens,
            )
            answer = response.choices[0].message.content.strip()
            return answer, citations, model_used
        except Exception as e:
            log.error(f"[web_search] Synthesis failed: {e}")
            # Fallback: return best snippets
            fallback = "\n\n".join(
                f"**{c.title}**\n{c.snippet}" for c in citations[:4]
            )
            return fallback, citations, "fallback"


# ─────────────────────────────────────────────────────────────────────────────
# Main SmartWebSearch orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class SmartWebSearch:
    """
    Full intelligent search pipeline.
    Wires together: QueryExpander → MultiSearch → SourceRanker →
                    ContentFetcher → Synthesizer
    """

    def __init__(self) -> None:
        self.expander  = QueryExpander()
        self.searcher  = MultiSearch()
        self.ranker    = SourceRanker()
        self.fetcher   = ContentFetcher()
        self.synth     = Synthesizer()

    async def search(
        self,
        query:         str,
        intent:        str       = "research",
        persona:       str       = "jarvis",
        emotion_state  = None,    # EmotionState | None
        telugu_state   = None,    # TeluguState  | None
        router         = None,
        max_tokens:    int       = 1200,
        expand_llm:    bool      = True,
    ) -> SearchResult:
        """
        Blocking search → returns complete SearchResult.
        Use stream() for real-time progress events.
        """
        t0 = time.monotonic()
        emotion_name = emotion_state.primary.value if emotion_state and not emotion_state.is_neutral else "neutral"

        # 1. Query expansion
        if router and expand_llm:
            queries = await self.expander.expand_llm(query, intent, router)
        else:
            queries = self.expander.expand_fast(query, intent)

        # 2. Multi-query search (parallel)
        raw_results, provider = await self.searcher.search_parallel(queries)
        if not raw_results:
            return SearchResult(
                synthesis=f"No web results found for: {query}",
                queries_used=queries,
                provider=provider,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )

        # 3. Rank
        ranked = self.ranker.rank(raw_results)

        # 4. Fetch top pages (top 3 ranked results)
        top_urls = [r.url for r in ranked[:4] if r.url and r.url.startswith("http")]
        page_contents = await self.fetcher.fetch_batch(top_urls, max_pages=3)
        for item in ranked:
            if item.url in page_contents:
                item.content = page_contents[item.url]

        # 5. Synthesize
        if router:
            synthesis, citations, model_used = await self.synth.synthesize(
                query=query,
                items=ranked[:6],
                persona=persona,
                emotion_name=emotion_name,
                router=router,
                max_tokens=max_tokens,
            )
        else:
            # No LLM — return formatted snippets
            synthesis = "\n\n".join(
                f"**{r.title}**\n{r.snippet}" for r in ranked[:5]
            )
            citations = [
                Citation(index=i+1, title=r.title, url=r.url, snippet=r.snippet)
                for i, r in enumerate(ranked[:5])
            ]
            model_used = "none"

        latency_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            f"[web_search] Done | queries={len(queries)} | "
            f"results={len(raw_results)} | provider={provider} | {latency_ms}ms"
        )

        return SearchResult(
            synthesis=synthesis,
            citations=citations,
            queries_used=queries,
            provider=provider,
            model_used=model_used,
            hops=len(queries),
            latency_ms=latency_ms,
            raw_results=ranked[:8],
        )

    async def stream(
        self,
        query:        str,
        intent:       str   = "research",
        persona:      str   = "jarvis",
        emotion_state = None,
        telugu_state  = None,
        router        = None,
        max_tokens:   int   = 1200,
    ) -> AsyncGenerator[dict, None]:
        """
        SSE-compatible streaming pipeline.
        Yields dicts:
          {type: "search_progress", step: "expanding"|"searching"|"reading"|"ranking"|"synthesising", message, queries?}
          {type: "search_result",   data: {synthesis, citations, queries_used, provider, latency_ms}}
          {type: "error",           message}
        """
        t0 = time.monotonic()
        emotion_name = emotion_state.primary.value if emotion_state and not emotion_state.is_neutral else "neutral"

        try:
            # Step 1: Query expansion
            yield {"type": "search_progress", "step": "expanding",
                   "message": "Expanding search query..."}
            if router:
                queries = await self.expander.expand_llm(query, intent, router)
            else:
                queries = self.expander.expand_fast(query, intent)
            yield {"type": "search_progress", "step": "expanding",
                   "message": f"Using {len(queries)} search angles", "queries": queries}

            # Step 2: Search
            yield {"type": "search_progress", "step": "searching",
                   "message": f"Searching: {queries[0][:60]}..."}
            raw_results, provider = await self.searcher.search_parallel(queries)
            if not raw_results:
                yield {"type": "search_result", "data": {
                    "synthesis": f"No web results found for: {query}",
                    "citations": [], "queries_used": queries,
                    "provider": provider, "latency_ms": int((time.monotonic() - t0) * 1000),
                }}
                return
            yield {"type": "search_progress", "step": "searching",
                   "message": f"Found {len(raw_results)} results via {provider}"}

            # Step 3: Rank
            ranked = self.ranker.rank(raw_results)

            # Step 4: Fetch top pages
            top_urls = [r.url for r in ranked[:4] if r.url and r.url.startswith("http")]
            for url in top_urls[:3]:
                short_url = url[:60] + "..." if len(url) > 60 else url
                yield {"type": "search_progress", "step": "reading",
                       "message": f"Reading: {short_url}", "url": url}
            page_contents = await self.fetcher.fetch_batch(top_urls, max_pages=3)
            for item in ranked:
                if item.url in page_contents:
                    item.content = page_contents[item.url]

            # Step 5: Synthesize
            yield {"type": "search_progress", "step": "synthesising",
                   "message": "Synthesising answer from sources..."}
            if router:
                synthesis, citations, model_used = await self.synth.synthesize(
                    query=query, items=ranked[:6], persona=persona,
                    emotion_name=emotion_name, router=router, max_tokens=max_tokens,
                )
            else:
                synthesis = "\n\n".join(f"**{r.title}**\n{r.snippet}" for r in ranked[:5])
                citations = [Citation(index=i+1, title=r.title, url=r.url, snippet=r.snippet)
                             for i, r in enumerate(ranked[:5])]
                model_used = "none"

            latency_ms = int((time.monotonic() - t0) * 1000)
            yield {
                "type": "search_result",
                "data": {
                    "synthesis":    synthesis,
                    "citations":    [{"index": c.index, "title": c.title, "url": c.url,
                                      "snippet": c.snippet, "date": c.date}
                                     for c in citations],
                    "queries_used": queries,
                    "provider":     provider,
                    "model_used":   model_used,
                    "latency_ms":   latency_ms,
                }
            }

        except Exception as e:
            log.error(f"[web_search] Stream pipeline error: {e}")
            yield {"type": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: SmartWebSearch | None = None


def get_web_search() -> SmartWebSearch:
    global _instance
    if _instance is None:
        _instance = SmartWebSearch()
    return _instance
