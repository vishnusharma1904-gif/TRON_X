"""
TRON-X Anticipation Engine  (Phase 37)
──────────────────────────────────────
"Sir, you have three meetings today, the 2pm overlaps with your dentist,
rain starts at 5, and there's an unread email from your bank."

Gathers context from every source TRON-X already has — calendar, email,
reminders, weather, news, home state, episodic memory — *in parallel*,
each behind its own timeout so one dead integration never stalls the
briefing. Then asks the LLM (via the existing orchestrator pipeline) to
compose it in persona.

Every section degrades gracefully: unauthenticated calendar, missing IMAP
creds, offline Home Assistant etc. simply drop out of the context block.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Optional

from src.core.config import get_settings
from src.core.logger import log
from src.core.event_bus import get_event_bus, EVT_BRIEFING

settings = get_settings()

_SECTION_TIMEOUT = 12.0   # seconds per source
_BRIEFING_CACHE_TTL = 300  # don't recompose within 5 minutes


async def _guard(name: str, coro) -> tuple[str, Any]:
    """Run one context source with a timeout; never raise."""
    try:
        return name, await asyncio.wait_for(coro, timeout=_SECTION_TIMEOUT)
    except Exception as e:
        log.debug(f"[anticipator] source '{name}' unavailable: {e}")
        return name, None


# ── Context sources (each returns a compact string or None) ───────────────────

async def _ctx_calendar() -> Optional[str]:
    from src.agents.calendar_agent import CalendarAgent
    cal = CalendarAgent()
    if not await cal.is_authenticated():
        return None
    result = await cal.list_events(days=1, max_results=10)
    events = result.get("events") or []
    if not events:
        return "Calendar: no events today."
    lines = ["Calendar (today):"]
    for ev in events[:10]:
        start = ev.get("start", "")
        lines.append(f"  - {start} {ev.get('summary', '(untitled)')}")
    conflicts = detect_conflicts(events)
    for a, b in conflicts:
        lines.append(
            f"  ⚠ CONFLICT: '{a.get('summary')}' overlaps '{b.get('summary')}'")
    return "\n".join(lines)


async def _ctx_email() -> Optional[str]:
    if not (settings.imap_host and settings.imap_user):
        return None
    from src.agents.email_agent import EmailAgent
    result = await EmailAgent().fetch_emails(limit=5, unread_only=True)
    emails = result.get("emails") or []
    if not emails:
        return "Email: inbox clear, no unread."
    lines = [f"Email: {len(emails)} unread (showing newest):"]
    for m in emails[:5]:
        lines.append(f"  - from {m.get('from', '?')}: {m.get('subject', '(no subject)')}")
    return "\n".join(lines)


async def _ctx_reminders() -> Optional[str]:
    from src.agents.reminder_agent import get_reminder_agent
    result = await get_reminder_agent().list_reminders()
    rems = result.get("reminders") or []
    if not rems:
        return None
    lines = [f"Pending reminders ({len(rems)}):"]
    for r in rems[:5]:
        lines.append(f"  - {r.get('fire_at', '?')}: {r.get('message', '')}")
    return "\n".join(lines)


async def _ctx_weather() -> Optional[str]:
    from src.feeds.weather import get_weather_feed
    loc = getattr(settings, "default_location", None) or "auto"
    cur = await get_weather_feed().current(loc)
    if cur.get("error"):
        return None
    return (f"Weather in {cur.get('location', loc)}: {cur.get('condition', '?')}, "
            f"{cur.get('temp', '?')}° (feels {cur.get('feels_like', '?')}°), "
            f"humidity {cur.get('humidity', '?')}%")


async def _ctx_news() -> Optional[str]:
    from src.feeds.news import get_news_feed
    result = await get_news_feed().headlines(count=5)
    arts = result.get("articles") or []
    if not arts:
        return None
    lines = ["Top headlines:"]
    for a in arts[:5]:
        lines.append(f"  - {a.get('title', '')}")
    return "\n".join(lines)


async def _ctx_home() -> Optional[str]:
    if not settings.ha_url:
        return None
    from src.iot.home_assistant import get_ha
    summary = await get_ha().home_summary()
    return f"Home status: {str(summary)[:400]}" if summary else None


async def _ctx_memory() -> Optional[str]:
    """What was on the user's mind recently (episodic topics)."""
    from src.memory.episodic_memory import EpisodicMemoryAgent
    result = await EpisodicMemoryAgent().list_episodes(days=2, limit=10)
    eps = result.get("episodes") or []
    topics = []
    for e in eps:
        t = (e.get("topic") or "").strip()
        if t and t not in topics:
            topics.append(t)
    if not topics:
        return None
    return "Recent conversation topics: " + ", ".join(topics[:8])


# ── Conflict detection ─────────────────────────────────────────────────────────

def _parse_dt(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def detect_conflicts(events: list[dict]) -> list[tuple[dict, dict]]:
    """Pairs of events whose [start, end) intervals overlap."""
    timed = []
    for ev in events:
        s = _parse_dt(str(ev.get("start", "")))
        e = _parse_dt(str(ev.get("end", "")))
        if s and e:
            timed.append((s, e, ev))
    timed.sort(key=lambda t: t[0])
    conflicts = []
    for i in range(len(timed) - 1):
        s1, e1, ev1 = timed[i]
        s2, _e2, ev2 = timed[i + 1]
        if s2 < e1:
            conflicts.append((ev1, ev2))
    return conflicts


# ── Engine ─────────────────────────────────────────────────────────────────────

class Anticipator:
    """Composes proactive briefings from all live context sources."""

    def __init__(self) -> None:
        self._last: dict[str, Any] = {}   # kind -> {"ts", "text", "context"}

    async def gather_context(self) -> dict[str, str]:
        """Fetch every source in parallel; return {name: text} for live ones."""
        results = await asyncio.gather(
            _guard("calendar",  _ctx_calendar()),
            _guard("email",     _ctx_email()),
            _guard("reminders", _ctx_reminders()),
            _guard("weather",   _ctx_weather()),
            _guard("news",      _ctx_news()),
            _guard("home",      _ctx_home()),
            _guard("memory",    _ctx_memory()),
        )
        return {name: text for name, text in results if text}

    async def briefing(self, kind: str = "morning",
                       persona: str = "jarvis",
                       force: bool = False) -> dict:
        """Compose a briefing of the given kind ('morning'|'evening'|'adhoc')."""
        cached = self._last.get(kind)
        if cached and not force and time.time() - cached["ts"] < _BRIEFING_CACHE_TTL:
            return {**cached, "cached": True}

        ctx = await self.gather_context()
        now = datetime.now().strftime("%A, %B %d %Y, %H:%M")

        if not ctx:
            text = ("All quiet — no calendar, mail, or feed sources are "
                    "configured or reachable right now.")
        else:
            block = "\n\n".join(ctx.values())
            tone = {
                "morning": "an energizing start-of-day briefing",
                "evening": "a calm end-of-day wrap-up (tomorrow's first event, "
                           "anything left undone)",
                "adhoc":   "a quick situational update",
            }.get(kind, "a situational update")
            prompt = (
                f"It is {now}. Compose {tone} for your principal from the "
                f"live data below. Be specific — names, times, numbers. "
                f"Lead with whatever is most urgent (conflicts and unread "
                f"items outrank weather). Skip sections with nothing notable. "
                f"Under 150 words, spoken-voice friendly, in persona.\n\n"
                f"=== LIVE DATA ===\n{block}"
            )
            from src.intelligence.orchestrator import get_orchestrator
            result = await get_orchestrator().chat(
                prompt, f"__proactive_{kind}__", "chat", persona,
                max_tokens=400, temperature=0.6,
            )
            text = result.get("reply", "").strip() or "Briefing unavailable."

        out = {"kind": kind, "ts": time.time(), "text": text,
               "sources": sorted(ctx.keys()), "cached": False}
        self._last[kind] = out
        get_event_bus().publish(
            EVT_BRIEFING, source="anticipator",
            kind=kind, text=text, sources=out["sources"],
        )
        log.info(f"[anticipator] {kind} briefing composed "
                 f"({len(ctx)} sources): {text[:80]}…")
        return out


# ── Singleton ──────────────────────────────────────────────────────────────────

_anticipator: Optional[Anticipator] = None


def get_anticipator() -> Anticipator:
    global _anticipator
    if _anticipator is None:
        _anticipator = Anticipator()
    return _anticipator
