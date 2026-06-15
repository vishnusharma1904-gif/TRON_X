"""
TRON-X Event Bus  (Phase 37 — Proactive Intelligence)
─────────────────────────────────────────────────────
Typed, async, in-process publish/subscribe bus.

Why: agents and proactive watchers should not call each other point-to-point.
Anything that happens in TRON-X (an intent classified, an agent finishing,
a device changing state, a proactive nudge firing) is published here as a
typed event. Subscribers — the HUD's live activity feed (SSE), the
Anticipation Engine, analytics — just listen.

Design notes
- Pure asyncio, no external broker. Single-process by design.
- `publish()` is non-blocking: subscriber callbacks are scheduled as tasks;
  queue subscribers receive via `asyncio.Queue` (bounded, drop-oldest).
- A ring buffer keeps the last `HISTORY_SIZE` events so late subscribers
  (e.g. the HUD reconnecting) can backfill.
- Thread-safety: publish_threadsafe() for non-async producers (APScheduler).
"""
from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from src.core.logger import log

HISTORY_SIZE = 200
QUEUE_SIZE = 100


# ── Event model ────────────────────────────────────────────────────────────────

class Event(BaseModel):
    """A single typed event on the bus."""
    id: int = 0                              # assigned by the bus, monotonic
    type: str                                # e.g. "agent.result", "proactive.trigger"
    source: str = "system"                   # module that emitted it
    ts: float = Field(default_factory=time.time)
    data: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        return f"[{self.type}] from {self.source}"


# Well-known event types (string constants, not an enum, so plugins can add more)
EVT_USER_UTTERANCE   = "user.utterance"
EVT_INTENT           = "intent.classified"
EVT_AGENT_START      = "agent.start"
EVT_AGENT_RESULT     = "agent.result"
EVT_MEMORY_WRITTEN   = "memory.written"
EVT_DEVICE_STATE     = "device.state_changed"
EVT_PROACTIVE        = "proactive.trigger"
EVT_BRIEFING         = "proactive.briefing"
EVT_CONSOLIDATION    = "memory.consolidated"
EVT_SYSTEM           = "system.notice"


# ── Bus ────────────────────────────────────────────────────────────────────────

Handler = Callable[[Event], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}      # type -> callbacks
        self._wildcard: list[Handler] = []                 # subscribe to all
        self._queues: list[tuple[Optional[set[str]], asyncio.Queue[Event]]] = []
        self._history: deque[Event] = deque(maxlen=HISTORY_SIZE)
        self._seq = itertools.count(1)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- lifecycle -------------------------------------------------------------

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the running loop so threads (APScheduler) can publish."""
        self._loop = loop or asyncio.get_event_loop()

    # -- subscribe -------------------------------------------------------------

    def subscribe(self, event_type: Optional[str], handler: Handler) -> None:
        """Register an async callback. event_type=None → all events."""
        if event_type is None:
            self._wildcard.append(handler)
        else:
            self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_queue(self, event_types: Optional[set[str]] = None) -> asyncio.Queue:
        """Get a bounded queue fed with matching events (None → all).
        Used by SSE endpoints. Drop-oldest on overflow so a stalled
        client can never block the bus."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.append((event_types, q))
        return q

    def unsubscribe_queue(self, q: asyncio.Queue) -> None:
        self._queues = [(t, qq) for (t, qq) in self._queues if qq is not q]

    # -- publish ---------------------------------------------------------------

    def publish(self, event_type: str, source: str = "system",
                **data: Any) -> Event:
        """Publish an event. Safe to call from any async context."""
        evt = Event(id=next(self._seq), type=event_type, source=source, data=data)
        self._history.append(evt)

        # async callbacks — fire-and-forget tasks
        for h in self._handlers.get(event_type, []) + self._wildcard:
            try:
                asyncio.get_running_loop().create_task(self._safe_call(h, evt))
            except RuntimeError:
                # no running loop (sync/test context) — skip callbacks
                pass

        # queue subscribers — drop-oldest, never block
        for types, q in self._queues:
            if types is not None and event_type not in types:
                continue
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    pass
        return evt

    def publish_threadsafe(self, event_type: str, source: str = "system",
                           **data: Any) -> None:
        """Publish from a non-async thread (e.g. APScheduler job thread)."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(
                lambda: self.publish(event_type, source, **data))
        else:
            # best effort: record into history at least
            evt = Event(id=next(self._seq), type=event_type,
                        source=source, data=data)
            self._history.append(evt)

    @staticmethod
    async def _safe_call(handler: Handler, evt: Event) -> None:
        try:
            await handler(evt)
        except Exception as e:  # never let a subscriber kill the bus
            log.warning(f"[event_bus] handler error on {evt.type}: {e}")

    # -- history ---------------------------------------------------------------

    def recent(self, limit: int = 50,
               event_types: Optional[set[str]] = None) -> list[Event]:
        evts = [e for e in self._history
                if event_types is None or e.type in event_types]
        return evts[-limit:]


# ── Singleton ──────────────────────────────────────────────────────────────────

_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
