"""
A.V.E.N.G.E.R.S Dispatcher
==========================
Routes every request to one of the 21 personas, runs that persona's
deterministic ops, then streams the conversational reply through the EXISTING
TRON-X orchestrator (`src.intelligence.orchestrator`) so the full pipeline --
intent classification, emotion detection, Telugu/Tenglish detection, RAG,
persona engine, CoT, smart-router failover, ChromaDB storage -- runs untouched.

Routing precedence:
  1. Explicit persona override from the client (clicking a node in the UI).
  2. Keyword routing table from the registry (strong domain phrases).
  3. Existing IntentClassifier verdict mapped through registry `intents`.
  4. Default: JARVIS.

Event protocol (async generator of dicts; the WebSocket layer forwards them):
  {"type": "agent_state", "id": <persona>, "state": "active"|"idle"|"error"}
  {"type": "ops",         "persona": ..., "summary": ..., "data": ...}
  {"type": "meta",  ...}      passthrough from orchestrator.chat_stream
  {"type": "text",  ...}      passthrough (token chunks)
  {"type": "done",  ...}      passthrough (+ persona fields added)
  {"type": "error", ...}
"""
from __future__ import annotations

import json
import re
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from src.core.logger import log
from src.avengers import ops
from src.avengers.registry import AVENGERS, female_voiced_personas

# ---------------------------------------------------------------------------
# Persona -> ops handler map (deterministic capability leg)
# ---------------------------------------------------------------------------

_OPS_HANDLERS: dict[str, Callable[[str], Awaitable[Optional[dict]]]] = {
    "oracle":   ops.oracle_ops.handle,
    "athena":   ops.crm_ops.athena,
    "zeus":     ops.crm_ops.zeus,
    "stark":    ops.stark_ops.handle,
    "steve":    ops.steve_ops.handle,
    "herald":   ops.herald_ops.handle,
    "vision":   ops.vision_ops.handle,
    "banner":   ops.banner_ops.handle,
    "ultron":   ops.ultron_ops.handle,
    "thor":     ops.thor_ops.handle,
    "atlas":    ops.atlas_ops.handle,
    "hercules": ops.hercules_ops.handle,
    "strange":  ops.strange_ops.handle,
    "spectre":  ops.spectre_ops.handle,
    "jalen":    ops.jalen_ops.handle,
    "ants":     ops.ants_ops.handle,
    "jerome":   ops.jerome_ops.handle,
    "hulk":     ops.hulk_ops.handle,
    "pepper":   ops.pepper_ops.handle,
    # jarvis & friday have no deterministic leg: the orchestrator pipeline
    # (web search for research intents, commands, RAG) IS their backend.
}

# Personas whose underlying TRON-X persona voice should be "friday" --
# single source of truth is the registry's gender metadata (see
# src/avengers/registry.py: female_voiced_personas()).
_FRIDAY_VOICED = female_voiced_personas()


# ---------------------------------------------------------------------------
# Agent mode -- autonomous multi-step continuation protocol
# ---------------------------------------------------------------------------
# When agent_mode is on, JARVIS (or whichever persona is currently speaking)
# can hand the next step of a multi-step objective to another A.V.E.N.G.E.R.S
# persona without the user re-prompting. The model is instructed to end its
# reply with a single machine-readable marker line that api/avengers.py
# parses after the "done" event to decide whether to loop dispatch_stream
# again (and with which persona / instruction), or stop.
_AGENT_MODE_PROMPT = """
[AGENT MODE -- AUTONOMOUS MULTI-STEP EXECUTION]
You are operating in agent mode: you may chain multiple A.V.E.N.G.E.R.S
personas to fully complete the user's objective without waiting for the
user to respond again.

After you finish addressing the CURRENT step, decide whether another step
by another persona is required to fully complete the user's overall goal.

- If MORE steps are needed, end your entire reply with exactly one line,
  on its own line, with nothing before or after it on that line:
  ###NEXT persona=<id> :: <clear, self-contained instruction for that persona>###
  where <id> is one of: {persona_ids}

- If the objective is FULLY complete and no further action is needed, end
  your entire reply with exactly:
  ###NEXT done###

Rules: always include exactly one ###NEXT ...### marker as the very last
line of your reply. Never mention or explain this marker to the user. Keep
chains short -- only request another step when genuinely necessary.
"""


def _agent_mode_instructions() -> str:
    return _AGENT_MODE_PROMPT.format(persona_ids=", ".join(AVENGERS.keys()))


def _intent_persona_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for pid, cfg in AVENGERS.items():
        for intent in cfg.get("intents", []):
            mapping.setdefault(intent, pid)
    return mapping


class AvengersDispatcher:

    def __init__(self) -> None:
        self._intent_map = _intent_persona_map()
        # Pre-compile keyword regexes, longest keyword first so specific
        # phrases beat generic ones.
        self._keyword_routes: list[tuple[re.Pattern, str]] = []
        pairs: list[tuple[str, str]] = []
        for pid, cfg in AVENGERS.items():
            for kw in cfg.get("keywords", []):
                pairs.append((kw, pid))
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        for kw, pid in pairs:
            self._keyword_routes.append(
                (re.compile(rf"\b{re.escape(kw)}\b", re.I), pid))
        log.info("[avengers] Dispatcher online -- %d personas, %d keyword routes",
                 len(AVENGERS), len(self._keyword_routes))

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def route(self, message: str, persona_override: str | None = None) -> str:
        if persona_override and persona_override in AVENGERS:
            return persona_override
        for pattern, pid in self._keyword_routes:
            if pattern.search(message):
                return pid
        # Fall back to the existing intent classifier (reuse the orchestrator's
        # instance so its intent cache and router stay shared).
        try:
            from src.intelligence.orchestrator import get_orchestrator
            intent, _conf, _method = await get_orchestrator().intent_clf.classify(message)
            if intent in self._intent_map:
                return self._intent_map[intent]
        except Exception as e:
            log.debug("[avengers] intent fallback failed: %s", e)
        return "jarvis"

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def dispatch_stream(
        self,
        message: str,
        session_id: str | None = None,
        persona_override: str | None = None,
        agent_mode: bool = False,
    ) -> AsyncGenerator[dict, None]:
        persona_id = await self.route(message, persona_override)
        cfg = AVENGERS[persona_id]
        yield {"type": "agent_state", "id": persona_id, "state": "active",
               "codename": cfg["codename"], "title": cfg["title"]}

        # 1. Deterministic ops leg ------------------------------------------------
        ops_result: dict | None = None
        handler = _OPS_HANDLERS.get(persona_id)
        if handler is not None:
            try:
                ops_result = await handler(message)
            except Exception as e:
                log.warning("[avengers] %s ops failed: %s", persona_id, e)
                yield {"type": "ops", "persona": persona_id,
                       "summary": f"ops error: {e}", "data": None}

        if ops_result is not None:
            yield {"type": "ops", "persona": persona_id,
                   "summary": ops_result["summary"], "data": ops_result.get("data")}

        # 2. Final ops -> speak the summary directly, skip the LLM ---------------
        if ops_result is not None and ops_result.get("final"):
            yield {"type": "meta", "intent": "avengers_ops", "persona": persona_id,
                   "session_id": session_id, "codename": cfg["codename"]}
            yield {"type": "text", "content": ops_result["summary"]}
            yield {"type": "done", "model": "avengers_ops", "latency_ms": 0,
                   "tokens_used": 0, "session_id": session_id,
                   "intent": "avengers_ops", "persona": persona_id,
                   "codename": cfg["codename"]}
            yield {"type": "agent_state", "id": persona_id, "state": "idle"}
            return

        # 3. Conversational leg through the EXISTING orchestrator ----------------
        #    (Telugu / emotion / RAG / persona / smart-router all preserved.)
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        extra_system = cfg["overlay"]
        if ops_result is not None:
            payload = ops_result.get("data")
            payload_json = ""
            if payload is not None:
                try:
                    payload_json = json.dumps(payload, default=str)[:3000]
                except (TypeError, ValueError):
                    payload_json = str(payload)[:3000]
            extra_system += (
                "\n\n[LIVE TOOL RESULT -- ground your answer ONLY in this data]\n"
                + ops_result["summary"][:2000]
                + (f"\nRaw: {payload_json}" if payload_json else "")
            )

        if agent_mode:
            extra_system += "\n\n" + _agent_mode_instructions()

        base_persona = "friday" if persona_id in _FRIDAY_VOICED else "jarvis"
        try:
            async for event in orch.chat_stream(
                message,
                session_id=session_id,
                persona=base_persona,
                extra_system=extra_system,
            ):
                event.setdefault("avenger", persona_id)
                event.setdefault("codename", cfg["codename"])
                yield event
        except Exception as e:
            log.error("[avengers] orchestrator stream failed: %s", e)
            yield {"type": "error", "message": str(e), "persona": persona_id}
            yield {"type": "agent_state", "id": persona_id, "state": "error"}
            return

        yield {"type": "agent_state", "id": persona_id, "state": "idle"}


_dispatcher: AvengersDispatcher | None = None


def get_dispatcher() -> AvengersDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = AvengersDispatcher()
    return _dispatcher
