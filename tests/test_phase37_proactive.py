"""
Phase 37 — Proactive Intelligence test suite.

Covers: event bus (pub/sub, queues, history, threadsafe), conflict
detection, sentinel dedup + emission, anticipator graceful degradation +
caching, consolidation orchestration, API endpoints (briefing, events,
SSE backfill, manual sentinel/consolidation).

No network, no LLM: orchestrator/agents are monkeypatched throughout.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import types
from unittest.mock import AsyncMock, patch

import pytest


def _fake_module(**attrs) -> types.ModuleType:
    """Build a stub module for sys.modules patching (so tests never import
    heavy deps like chromadb / litellm that the real modules pull in)."""
    mod = types.ModuleType("fake")
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _stub_memory(fake_epi, fake_chroma):
    return patch.dict(sys.modules, {
        "src.memory.episodic_memory": _fake_module(
            EpisodicMemoryAgent=lambda: fake_epi),
        "src.memory.chroma_db": _fake_module(
            get_chroma=lambda: fake_chroma),
    })


def _stub_orchestrator(fake_orch):
    return patch.dict(sys.modules, {
        "src.intelligence.orchestrator": _fake_module(
            get_orchestrator=lambda: fake_orch),
    })

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core import event_bus as eb_mod
from src.core.event_bus import (
    EventBus, Event, EVT_PROACTIVE, EVT_BRIEFING, EVT_CONSOLIDATION,
    EVT_AGENT_RESULT, get_event_bus,
)


@pytest.fixture()
def bus():
    """Fresh isolated bus per test (and reset the singleton)."""
    eb_mod._bus = None
    return get_event_bus()


# ═══════════════════════════════════════════════════════════════════════════
# Event bus
# ═══════════════════════════════════════════════════════════════════════════

class TestEventBus:
    def test_publish_returns_typed_event(self, bus):
        evt = bus.publish("x.y", source="t", a=1)
        assert isinstance(evt, Event)
        assert evt.type == "x.y" and evt.source == "t" and evt.data == {"a": 1}
        assert evt.id == 1

    def test_ids_monotonic(self, bus):
        ids = [bus.publish("t").id for _ in range(5)]
        assert ids == sorted(ids) and len(set(ids)) == 5

    def test_history_ring_and_filter(self, bus):
        for i in range(10):
            bus.publish("a" if i % 2 else "b", n=i)
        assert len(bus.recent(limit=50)) == 10
        only_a = bus.recent(event_types={"a"})
        assert all(e.type == "a" for e in only_a) and len(only_a) == 5
        assert len(bus.recent(limit=3)) == 3

    def test_history_bounded(self, bus):
        for _ in range(eb_mod.HISTORY_SIZE + 50):
            bus.publish("t")
        assert len(bus.recent(limit=10_000)) == eb_mod.HISTORY_SIZE

    @pytest.mark.asyncio
    async def test_async_subscriber_receives(self, bus):
        got = []

        async def handler(evt: Event):
            got.append(evt.type)

        bus.subscribe("ping", handler)
        bus.publish("ping")
        bus.publish("other")          # not subscribed
        await asyncio.sleep(0.05)
        assert got == ["ping"]

    @pytest.mark.asyncio
    async def test_wildcard_subscriber(self, bus):
        got = []

        async def handler(evt: Event):
            got.append(evt.type)

        bus.subscribe(None, handler)
        bus.publish("a"); bus.publish("b")
        await asyncio.sleep(0.05)
        assert got == ["a", "b"]

    @pytest.mark.asyncio
    async def test_crashing_subscriber_never_breaks_bus(self, bus):
        async def bad(evt):
            raise RuntimeError("boom")

        ok = []

        async def good(evt):
            ok.append(1)

        bus.subscribe("t", bad)
        bus.subscribe("t", good)
        bus.publish("t")
        await asyncio.sleep(0.05)
        assert ok == [1]

    @pytest.mark.asyncio
    async def test_queue_subscriber_and_filter(self, bus):
        q = bus.subscribe_queue({"keep"})
        bus.publish("keep"); bus.publish("drop")
        evt = q.get_nowait()
        assert evt.type == "keep" and q.empty()
        bus.unsubscribe_queue(q)
        bus.publish("keep")
        assert q.empty()

    @pytest.mark.asyncio
    async def test_queue_overflow_drops_oldest(self, bus):
        q = bus.subscribe_queue()
        for i in range(eb_mod.QUEUE_SIZE + 5):
            bus.publish("t", n=i)
        # oldest dropped: first item should be n=5
        first = q.get_nowait()
        assert first.data["n"] == 5

    def test_threadsafe_no_loop_still_records_history(self, bus):
        bus.publish_threadsafe("t", source="thread")
        assert bus.recent()[-1].source == "thread"


# ═══════════════════════════════════════════════════════════════════════════
# Conflict detection
# ═══════════════════════════════════════════════════════════════════════════

def _ev(summary, start_h, end_h, day="2026-06-11"):
    return {"id": summary, "summary": summary,
            "start": f"{day}T{start_h:02d}:00:00+00:00",
            "end":   f"{day}T{end_h:02d}:00:00+00:00"}


class TestConflictDetection:
    def test_overlap_detected(self):
        from src.proactive.anticipator import detect_conflicts
        out = detect_conflicts([_ev("A", 14, 15), _ev("B", 14, 16)])
        assert len(out) == 1
        assert {out[0][0]["summary"], out[0][1]["summary"]} == {"A", "B"}

    def test_back_to_back_is_not_conflict(self):
        from src.proactive.anticipator import detect_conflicts
        assert detect_conflicts([_ev("A", 9, 10), _ev("B", 10, 11)]) == []

    def test_unparseable_dates_skipped(self):
        from src.proactive.anticipator import detect_conflicts
        assert detect_conflicts([{"summary": "X", "start": "??", "end": ""},
                                 _ev("A", 9, 10)]) == []

    def test_unsorted_input(self):
        from src.proactive.anticipator import detect_conflicts
        out = detect_conflicts([_ev("B", 14, 16), _ev("A", 13, 15)])
        assert len(out) == 1


# ═══════════════════════════════════════════════════════════════════════════
# Sentinel
# ═══════════════════════════════════════════════════════════════════════════

class TestSentinel:
    def _fresh_sentinel(self):
        from src.proactive import triggers as trig
        trig._sentinel = None
        return trig.get_sentinel()

    def test_dedup_within_ttl(self, bus):
        s = self._fresh_sentinel()
        assert s._emit("k1", "test", "T", "B") is True
        assert s._emit("k1", "test", "T", "B") is False   # suppressed
        assert s._emit("k2", "test", "T", "B") is True

    def test_emit_publishes_event(self, bus):
        s = self._fresh_sentinel()
        s._emit("k", "calendar", "Title", "Body", urgency="high")
        evts = bus.recent(event_types={EVT_PROACTIVE})
        assert len(evts) == 1
        assert evts[0].data["title"] == "Title"
        assert evts[0].data["urgency"] == "high"

    def test_dedup_ttl_expiry(self, bus):
        s = self._fresh_sentinel()
        s._emit("k", "t", "T", "B")
        s._seen["k"] -= (6 * 3600 + 1)   # age past TTL
        assert s._emit("k", "t", "T", "B") is True

    @pytest.mark.asyncio
    async def test_meeting_soon_fires_within_lead(self, bus):
        s = self._fresh_sentinel()
        soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        fake = {"events": [{"id": "e1", "summary": "Standup", "start": soon,
                            "end": soon}]}
        with patch("src.agents.calendar_agent.CalendarAgent") as MockCal:
            inst = MockCal.return_value
            inst.is_authenticated = AsyncMock(return_value=True)
            inst.list_events = AsyncMock(return_value=fake)
            fired = await s.check_meetings_soon()
        assert fired == 1
        evts = bus.recent(event_types={EVT_PROACTIVE})
        assert "Standup" in evts[0].data["title"]

    @pytest.mark.asyncio
    async def test_meeting_far_away_does_not_fire(self, bus):
        s = self._fresh_sentinel()
        later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        fake = {"events": [{"id": "e1", "summary": "X", "start": later,
                            "end": later}]}
        with patch("src.agents.calendar_agent.CalendarAgent") as MockCal:
            inst = MockCal.return_value
            inst.is_authenticated = AsyncMock(return_value=True)
            inst.list_events = AsyncMock(return_value=fake)
            assert await s.check_meetings_soon() == 0

    @pytest.mark.asyncio
    async def test_unauthenticated_calendar_is_silent(self, bus):
        s = self._fresh_sentinel()
        with patch("src.agents.calendar_agent.CalendarAgent") as MockCal:
            MockCal.return_value.is_authenticated = AsyncMock(return_value=False)
            assert await s.check_meetings_soon() == 0
            assert await s.check_conflicts() == 0

    @pytest.mark.asyncio
    async def test_run_once_isolates_crashing_check(self, bus):
        s = self._fresh_sentinel()
        with patch.object(s, "check_meetings_soon",
                          AsyncMock(side_effect=RuntimeError("x"))), \
             patch.object(s, "check_conflicts", AsyncMock(return_value=2)), \
             patch.object(s, "check_vip_email", AsyncMock(return_value=0)):
            counts = await s.run_once()
        assert counts == {"meetings_soon": 0, "conflicts": 2, "vip_email": 0}


# ═══════════════════════════════════════════════════════════════════════════
# Anticipator
# ═══════════════════════════════════════════════════════════════════════════

class TestAnticipator:
    def _fresh(self):
        from src.proactive import anticipator as ant
        ant._anticipator = None
        return ant.get_anticipator()

    @pytest.mark.asyncio
    async def test_all_sources_dead_yields_quiet_briefing(self, bus):
        a = self._fresh()
        with patch.object(a, "gather_context", AsyncMock(return_value={})):
            out = await a.briefing(kind="adhoc", force=True)
        assert out["sources"] == []
        assert "quiet" in out["text"].lower()
        # still publishes the briefing event
        assert bus.recent(event_types={EVT_BRIEFING})

    @pytest.mark.asyncio
    async def test_briefing_composes_via_orchestrator(self, bus):
        a = self._fresh()
        ctx = {"weather": "Weather: sunny 31°", "calendar": "Calendar: free"}
        fake_orch = AsyncMock()
        fake_orch.chat = AsyncMock(return_value={"reply": "Good morning, sir."})
        with patch.object(a, "gather_context", AsyncMock(return_value=ctx)), \
             _stub_orchestrator(fake_orch):
            out = await a.briefing(kind="morning", force=True)
        assert out["text"] == "Good morning, sir."
        assert sorted(out["sources"]) == ["calendar", "weather"]
        # the live data block must reach the LLM
        prompt = fake_orch.chat.call_args[0][0]
        assert "sunny 31°" in prompt

    @pytest.mark.asyncio
    async def test_briefing_cache(self, bus):
        a = self._fresh()
        fake_orch = AsyncMock()
        fake_orch.chat = AsyncMock(return_value={"reply": "Hello."})
        with patch.object(a, "gather_context",
                          AsyncMock(return_value={"x": "y"})) as gc, \
             _stub_orchestrator(fake_orch):
            first = await a.briefing(kind="morning", force=True)
            second = await a.briefing(kind="morning")          # cached
            assert second["cached"] is True
            assert gc.await_count == 1
            third = await a.briefing(kind="morning", force=True)
            assert third["cached"] is False

    @pytest.mark.asyncio
    async def test_gather_context_source_crash_is_isolated(self, bus):
        """A raising source must not poison the others."""
        from src.proactive.anticipator import _guard

        async def ok():
            return "fine"

        async def boom():
            raise RuntimeError("dead integration")

        results = dict(await asyncio.gather(_guard("a", ok()),
                                            _guard("b", boom())))
        assert results["a"] == "fine" and results["b"] is None


# ═══════════════════════════════════════════════════════════════════════════
# Consolidation
# ═══════════════════════════════════════════════════════════════════════════

class TestConsolidation:
    @pytest.mark.asyncio
    async def test_promotes_recurring_topics_and_publishes(self, bus):
        from src.proactive.consolidation import consolidate
        episodes = {"episodes": [{"topic": "rust"}, {"topic": "rust"},
                                 {"topic": "rust"}, {"topic": "one-off"}]}
        fake_epi = AsyncMock()
        fake_epi.period_summary = AsyncMock(return_value={"summary": "S"})
        fake_epi.list_episodes = AsyncMock(return_value=episodes)
        fake_epi.forget_before = AsyncMock(return_value={"count": 7})
        fake_chroma = AsyncMock()
        with _stub_memory(fake_epi, fake_chroma):
            stats = await consolidate(retention_days=30, prune=True)
        assert stats["summary"] == "S"
        assert stats["promoted"] == [{"topic": "rust", "count": 3}]
        assert stats["pruned"] == 7
        assert stats["errors"] == []
        fake_chroma.remember_fact.assert_awaited_once()
        assert bus.recent(event_types={EVT_CONSOLIDATION})

    @pytest.mark.asyncio
    async def test_prune_disabled_by_default_flag(self, bus):
        from src.proactive.consolidation import consolidate
        fake_epi = AsyncMock()
        fake_epi.period_summary = AsyncMock(return_value={"summary": "S"})
        fake_epi.list_episodes = AsyncMock(return_value={"episodes": []})
        with _stub_memory(fake_epi, AsyncMock()):
            stats = await consolidate(prune=False)
        assert stats["pruned"] == 0
        fake_epi.forget_before.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_subsystem_failure_recorded_not_raised(self, bus):
        from src.proactive.consolidation import consolidate
        fake_epi = AsyncMock()
        fake_epi.period_summary = AsyncMock(side_effect=RuntimeError("db down"))
        fake_epi.list_episodes = AsyncMock(return_value={"episodes": []})
        with _stub_memory(fake_epi, AsyncMock()):
            stats = await consolidate(prune=False)
        assert any("summary" in e for e in stats["errors"])


# ═══════════════════════════════════════════════════════════════════════════
# API endpoints
# ═══════════════════════════════════════════════════════════════════════════

class TestAPI:
    @pytest.fixture()
    def client(self, bus):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.api.proactive import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_events_endpoint(self, client, bus):
        bus.publish("a.b", source="t", k="v")
        r = client.get("/api/proactive/events")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["events"][0]["type"] == "a.b"

    def test_events_type_filter(self, client, bus):
        bus.publish("keep"); bus.publish("drop")
        r = client.get("/api/proactive/events", params={"types": "keep"})
        assert [e["type"] for e in r.json()["events"]] == ["keep"]

    def test_briefing_endpoint(self, client, bus):
        from src.proactive import anticipator as ant
        ant._anticipator = None
        a = ant.get_anticipator()
        with patch.object(a, "gather_context", AsyncMock(return_value={})):
            r = client.get("/api/proactive/briefing")
        assert r.status_code == 200
        assert r.json()["kind"] == "adhoc"

    def test_briefing_rejects_bad_kind(self, client):
        assert client.get("/api/proactive/briefing",
                          params={"kind": "nope"}).status_code == 422

    def test_sentinel_run_endpoint(self, client, bus):
        from src.proactive import triggers as trig
        trig._sentinel = None
        s = trig.get_sentinel()
        with patch.object(s, "check_meetings_soon", AsyncMock(return_value=1)), \
             patch.object(s, "check_conflicts", AsyncMock(return_value=0)), \
             patch.object(s, "check_vip_email", AsyncMock(return_value=0)):
            r = client.post("/api/proactive/sentinel/run")
        assert r.status_code == 200
        assert r.json()["meetings_soon"] == 1

    def test_consolidate_endpoint(self, client, bus):
        fake_epi = AsyncMock()
        fake_epi.period_summary = AsyncMock(return_value={"summary": "S"})
        fake_epi.list_episodes = AsyncMock(return_value={"episodes": []})
        with _stub_memory(fake_epi, AsyncMock()):
            r = client.post("/api/proactive/consolidate",
                            json={"prune": False})
        assert r.status_code == 200
        assert r.json()["summary"] == "S"

    @pytest.mark.asyncio
    async def test_sse_stream_backfills(self, bus):
        """Consume the SSE generator directly (TestClient can't close an
        infinite stream cleanly), assert backfill frame + live frame."""
        from src.api.proactive import stream
        bus.publish("hello", source="t")
        resp = await stream(types=None, backfill=5)
        gen = resp.body_iterator
        first = await gen.__anext__()             # backfilled event
        assert "hello" in first
        bus.publish("live.event", source="t")     # arrives via queue
        second = await asyncio.wait_for(gen.__anext__(), timeout=2)
        assert "live.event" in second
        await gen.aclose()                        # triggers unsubscribe
        assert bus._queues == []
