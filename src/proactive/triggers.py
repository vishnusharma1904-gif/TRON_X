"""
TRON-X Proactive Sentinel  (Phase 37)
─────────────────────────────────────
The watcher loop. Runs every N seconds (scheduler interval job) and checks
for situations worth *interrupting you* about:

  - meeting starting within the lead-time window ("your 2pm is in 12 minutes")
  - calendar conflicts appearing today
  - new unread email from a VIP sender

Each finding is published on the event bus as EVT_PROACTIVE — the HUD
shows it as a card, and any other subscriber (TTS, Telegram, ntfy) can
act on it. Deduplicated: the same finding never fires twice within its
TTL window.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from src.core.config import get_settings
from src.core.logger import log
from src.core.event_bus import get_event_bus, EVT_PROACTIVE

settings = get_settings()

_DEDUP_TTL = 6 * 3600   # same alert key silent for 6h


class Sentinel:
    def __init__(self) -> None:
        self._seen: dict[str, float] = {}   # alert key -> last fired ts

    # -- dedup -----------------------------------------------------------------

    def _fresh(self, key: str) -> bool:
        now = time.time()
        # prune old keys
        self._seen = {k: t for k, t in self._seen.items()
                      if now - t < _DEDUP_TTL}
        if key in self._seen:
            return False
        self._seen[key] = now
        return True

    def _emit(self, key: str, category: str, title: str, body: str,
              urgency: str = "info") -> bool:
        if not self._fresh(key):
            return False
        get_event_bus().publish(
            EVT_PROACTIVE, source="sentinel",
            category=category, title=title, body=body, urgency=urgency,
        )
        log.info(f"[sentinel] {urgency.upper()} {category}: {title}")
        return True

    # -- checks ----------------------------------------------------------------

    async def check_meetings_soon(self) -> int:
        """Alert when an event starts within the lead window."""
        from src.agents.calendar_agent import CalendarAgent
        from src.proactive.anticipator import _parse_dt
        cal = CalendarAgent()
        if not await cal.is_authenticated():
            return 0
        result = await cal.list_events(days=1, max_results=10)
        events = result.get("events") or []
        lead = settings.proactive_meeting_lead_min
        now = datetime.now(timezone.utc)
        fired = 0
        for ev in events:
            start = _parse_dt(str(ev.get("start", "")))
            if not start:
                continue
            mins = (start - now).total_seconds() / 60
            if 0 < mins <= lead:
                key = f"meeting:{ev.get('id', ev.get('summary'))}:{ev.get('start')}"
                if self._emit(key, "calendar",
                              f"{ev.get('summary', 'Meeting')} in {int(mins)} min",
                              f"Starts at {ev.get('start')}", urgency="high"):
                    fired += 1
        return fired

    async def check_conflicts(self) -> int:
        from src.agents.calendar_agent import CalendarAgent
        from src.proactive.anticipator import detect_conflicts
        cal = CalendarAgent()
        if not await cal.is_authenticated():
            return 0
        result = await cal.list_events(days=1, max_results=20)
        conflicts = detect_conflicts(result.get("events") or [])
        fired = 0
        for a, b in conflicts:
            key = f"conflict:{a.get('id')}:{b.get('id')}"
            if self._emit(key, "calendar", "Calendar conflict today",
                          f"'{a.get('summary')}' overlaps '{b.get('summary')}'",
                          urgency="high"):
                fired += 1
        return fired

    async def check_vip_email(self) -> int:
        """Unread mail whose sender matches the configured VIP list."""
        vips = [v.strip().lower()
                for v in (settings.proactive_vip_senders or "").split(",")
                if v.strip()]
        if not vips or not (settings.imap_host and settings.imap_user):
            return 0
        from src.agents.email_agent import EmailAgent
        result = await EmailAgent().fetch_emails(limit=10, unread_only=True)
        fired = 0
        for m in result.get("emails") or []:
            sender = str(m.get("from", "")).lower()
            if any(v in sender for v in vips):
                key = f"vip_mail:{m.get('uid', m.get('subject'))}"
                if self._emit(key, "email",
                              f"Unread from {m.get('from', 'VIP')}",
                              m.get("subject", "(no subject)"),
                              urgency="high"):
                    fired += 1
        return fired

    # -- loop entry ------------------------------------------------------------

    async def run_once(self) -> dict:
        """One sentinel sweep. Each check is independent and crash-proof."""
        counts = {}
        for name, check in (
            ("meetings_soon", self.check_meetings_soon),
            ("conflicts",     self.check_conflicts),
            ("vip_email",     self.check_vip_email),
        ):
            try:
                counts[name] = await check()
            except Exception as e:
                log.debug(f"[sentinel] check '{name}' failed: {e}")
                counts[name] = 0
        return counts


# ── Singleton ──────────────────────────────────────────────────────────────────

_sentinel: Optional[Sentinel] = None


def get_sentinel() -> Sentinel:
    global _sentinel
    if _sentinel is None:
        _sentinel = Sentinel()
    return _sentinel
