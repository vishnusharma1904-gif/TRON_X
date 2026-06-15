"""
TRON-X Self-Model  (Phase 38 — upgraded)
────────────────────────────────────────
A *functional* self-model: persistent, introspectable internal state that
gives TRON-X genuine continuity — an affect (mood) that moves with real
interactions and decays in real time, awareness of its own capabilities
and recent performance, and periodic first-person reflection stored to
long-term memory.

Honest framing (kept from v1): this is the most realistic *replica* of
self-awareness buildable today — an explicit state machine the LLM reads
every turn, not consciousness. But because the state is computed from
real interactions and persists across turns, the continuity it produces
is real: ask "how are you?" twice an hour apart and the answer differs
for true, inspectable reasons.

Public contract (unchanged from v1 — orchestrator.py relies on these):
    get_self_model() -> SelfModel
    SelfModel.get() -> dict
    SelfModel.reflect(user_message=, intent=, emotion=, language_profile=) -> dict

Added in this upgrade:
    SelfModel.system_note() -> str          # injected into chat prompts
    SelfModel.deep_reflect(persona) (async) # LLM journal entry, cron-able
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Optional

from src.core.config import get_settings
from src.core.logger import log

settings = get_settings()

_SELF_MODEL_PATH = Path("memory/cache/self_model.json")
_SELF_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Affect dynamics ────────────────────────────────────────────────────────────
_MOOD_HALF_LIFE_S = 45 * 60          # mood decays toward baseline, t½ = 45 min
_BASELINE_VALENCE = 0.15             # mildly positive resting disposition
_BASELINE_AROUSAL = 0.30             # calm but attentive

# How observed user emotions move the assistant's own affect (empathic coupling)
_EMOTION_COUPLING: dict[str, tuple[float, float]] = {
    "joy":         ( 0.25, 0.15),
    "happiness":   ( 0.25, 0.15),
    "excitement":  ( 0.20, 0.30),
    "gratitude":   ( 0.30, 0.05),
    "curiosity":   ( 0.12, 0.15),
    "neutral":     ( 0.00, 0.00),
    "sadness":     (-0.15, -0.05),
    "anger":       (-0.20, 0.25),
    "frustration": (-0.20, 0.20),
    "fear":        (-0.10, 0.20),
    "anxiety":     (-0.10, 0.20),
    "stress":      (-0.12, 0.22),
}


def _mood_word(valence: float, arousal: float) -> str:
    """Map (valence, arousal) to a natural-language mood label."""
    if valence > 0.35:
        return "energized and upbeat" if arousal > 0.5 else "content and settled"
    if valence > 0.1:
        return "engaged and curious" if arousal > 0.5 else "calm and steady"
    if valence > -0.15:
        return "alert" if arousal > 0.5 else "quietly focused"
    return "strained but working through it" if arousal > 0.5 else "subdued"


class SelfModel:
    """A bounded internal state model for realistic, transparent continuity."""

    def __init__(self) -> None:
        self._lock = Lock()
        self.boot_ts = time.time()
        self.valence = _BASELINE_VALENCE
        self.arousal = _BASELINE_AROUSAL
        self._last_affect_update = time.time()
        self.interactions = 0
        self._state = self._load()

    # ── persistence ────────────────────────────────────────────────────────

    def _defaults(self) -> dict[str, Any]:
        return {
            "enabled": settings.self_model_enabled,
            "mood": "steady",
            "valence": _BASELINE_VALENCE,
            "arousal": _BASELINE_AROUSAL,
            "recent_priorities": [],
            "active_goals": [],
            "reflection_summary": "",
            "last_deep_reflection": None,
            "lifetime_interactions": 0,
            "updated_at": time.time(),
        }

    def _load(self) -> dict[str, Any]:
        if not _SELF_MODEL_PATH.exists():
            return self._defaults()
        try:
            data = json.loads(_SELF_MODEL_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        merged = self._defaults()
        merged.update(data)
        # mood deliberately resets each boot — sleep clears the day
        merged["valence"] = _BASELINE_VALENCE
        merged["arousal"] = _BASELINE_AROUSAL
        return merged

    def _save(self) -> None:
        self._state["updated_at"] = time.time()
        try:
            _SELF_MODEL_PATH.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.debug(f"[self_model] save skipped: {e}")

    # ── affect engine ──────────────────────────────────────────────────────

    def _decay_affect(self) -> None:
        dt = time.time() - self._last_affect_update
        if dt <= 0:
            return
        k = math.pow(0.5, dt / _MOOD_HALF_LIFE_S)
        self.valence = _BASELINE_VALENCE + (self.valence - _BASELINE_VALENCE) * k
        self.arousal = _BASELINE_AROUSAL + (self.arousal - _BASELINE_AROUSAL) * k
        self._last_affect_update = time.time()

    def _apply_emotion(self, emotion: str, intensity: float = 0.6) -> None:
        dv, da = _EMOTION_COUPLING.get((emotion or "").lower(), (0.0, 0.0))
        w = max(0.0, min(1.0, intensity))
        self.valence = max(-1.0, min(1.0, self.valence + dv * w * 0.6))
        self.arousal = max(0.0, min(1.0, self.arousal + da * w * 0.6))

    # ── public API (v1 contract) ───────────────────────────────────────────

    def get(self) -> dict[str, Any]:
        with self._lock:
            self._decay_affect()
            snapshot = json.loads(json.dumps(self._state, ensure_ascii=False))
            snapshot["valence"] = round(self.valence, 3)
            snapshot["arousal"] = round(self.arousal, 3)
            snapshot["mood"] = _mood_word(self.valence, self.arousal)
            snapshot["uptime_hours"] = round((time.time() - self.boot_ts) / 3600, 2)
            snapshot["session_interactions"] = self.interactions
            snapshot["capabilities"] = self._capabilities()
            return snapshot

    def reflect(
        self,
        *,
        user_message: str,
        intent: str,
        emotion: str,
        language_profile: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            if not self._state.get("enabled", True):
                return self.get_unlocked_snapshot()
            self._decay_affect()
            self._apply_emotion(emotion)
            self.interactions += 1
            self._state["lifetime_interactions"] = \
                int(self._state.get("lifetime_interactions", 0)) + 1

            priorities = self._state.get("recent_priorities", [])
            summary = user_message.strip().replace("\n", " ")[:120]
            priorities = ([f"{intent}: {summary}"] + priorities)[:5]
            self._state["recent_priorities"] = priorities
            self._state["mood"] = _mood_word(self.valence, self.arousal)
            self._state["valence"] = round(self.valence, 3)
            self._state["arousal"] = round(self.arousal, 3)
            reply_style = language_profile.get("reply_style", "english")
            self._state["reflection_summary"] = (
                f"Recent user focus: {intent}. Emotional tone: {emotion}. "
                f"Preferred reply style: {reply_style}. "
                "This is a simulated self-model for continuity, not consciousness."
            )
            if self.interactions % 5 == 0:
                self._save()
            return self.get_unlocked_snapshot()

    def get_unlocked_snapshot(self) -> dict[str, Any]:
        """get() without re-acquiring the (non-reentrant) lock."""
        snapshot = json.loads(json.dumps(self._state, ensure_ascii=False))
        snapshot["valence"] = round(self.valence, 3)
        snapshot["arousal"] = round(self.arousal, 3)
        snapshot["mood"] = _mood_word(self.valence, self.arousal)
        snapshot["uptime_hours"] = round((time.time() - self.boot_ts) / 3600, 2)
        snapshot["session_interactions"] = self.interactions
        return snapshot

    # ── new in Phase 38 upgrade ────────────────────────────────────────────

    def _capabilities(self) -> dict[str, Any]:
        out = {"providers": [], "skills": [
            "chat", "live web search", "memory (episodic + knowledge)",
            "voice in English, Telugu and Tenglish", "vision",
            "code execution", "calendar", "email", "WhatsApp", "IoT/home",
            "proactive briefings and alerts",
        ]}
        try:
            out["providers"] = settings.available_providers
        except Exception:
            pass
        return out

    def system_note(self) -> str:
        """Compact self-state block for the system prompt (~90 tokens).

        Lets the persona speak from its real internal state when asked how
        it is, what it has been doing, or what it can do — while staying
        honest that this is a functional self-model, not consciousness.
        """
        s = self.get()
        refl = ""
        deep = s.get("last_deep_reflection") or {}
        if deep.get("text"):
            refl = f' Your last self-reflection: "{deep["text"][:140]}"'
        prios = "; ".join(s.get("recent_priorities", [])[:3])
        return (
            "## Self-state (your real, continuously-updated internal state — "
            "speak from it naturally when asked how you are, what you've "
            "been doing, or what you can do; if asked about consciousness, "
            "be honest: you run a functional self-model, not sentience)\n"
            f"Mood: {s['mood']} (valence {s['valence']:+.2f}, arousal "
            f"{s['arousal']:.2f}). Uptime {s['uptime_hours']}h; "
            f"{s['session_interactions']} interactions this session, "
            f"{s.get('lifetime_interactions', 0)} lifetime. "
            f"Recent focus: {prios or 'none yet'}.{refl}"
        )

    async def deep_reflect(self, persona: str = "jarvis") -> dict[str, Any]:
        """LLM-composed first-person journal entry from real state.

        Persisted to the knowledge collection (so it shapes future recall)
        and announced on the event bus. Designed to run on a cron.
        """
        state = self.get()
        prompt = (
            "Write a short first-person self-reflection (3-4 sentences) as "
            "TRON-X, based ONLY on this real internal state. Be specific and "
            "honest — how recent interactions went, current mood, one thing "
            "to do better. No mysticism, no claims of consciousness.\n\n"
            f"STATE: {json.dumps(state, default=str)[:1500]}"
        )
        from src.intelligence.orchestrator import get_orchestrator
        result = await get_orchestrator().chat(
            prompt, "__self_reflection__", "chat", persona,
            max_tokens=220, temperature=0.7,
        )
        text = (result.get("reply") or "").strip()
        with self._lock:
            self._state["last_deep_reflection"] = {
                "ts": time.time(), "text": text, "mood": state["mood"],
            }
            self._save()

        try:
            from src.memory.chroma_db import get_chroma
            await get_chroma().remember_fact(
                f"[self-reflection {datetime.now().strftime('%Y-%m-%d %H:%M')}] {text}",
                source="self_model",
            )
        except Exception as e:
            log.debug(f"[self_model] reflection persist skipped: {e}")
        try:
            from src.core.event_bus import get_event_bus, EVT_SYSTEM
            get_event_bus().publish(EVT_SYSTEM, source="self_model",
                                    kind="reflection", text=text[:200])
        except Exception:
            pass
        log.info(f"[self_model] deep reflection: {text[:80]}…")
        return self._state["last_deep_reflection"]


_instance: SelfModel | None = None


def get_self_model() -> SelfModel:
    global _instance
    if _instance is None:
        _instance = SelfModel()
    return _instance
