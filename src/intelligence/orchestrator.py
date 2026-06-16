"""
TRON-X Orchestrator  v7  (Phase 5 — Full Integration)
─────────────────────────────────────────────────────────────
Full pipeline per request:
  1. Intent classification   (keyword fast-path -> LLM verification)
  2. Emotion detection       (pattern-based, zero latency)
  3. Telugu language detect  (script / romanised / tenglish / hyderabadi)
  2c. Computer control       (VisualComputerAgent fast-path for computer intent)
  3b. Web search             (SmartWebSearch for research / live-data intents)
  4. RAG retrieval           (ChromaDB semantic search if intent warrants it)
  5. Persona system prompt   (Jarvis / Friday + intent + emotion + Telugu + RAG)
  6. CoT injection           (for academic / medical / math / reasoning)
  7. Context window trim     (keep under token budget)
  8. Smart router            (provider failover)
  9. Response post-processing (strip think blocks, strip filler)
  10. Store turn in ChromaDB (for future recall)
  11. Session persistence    (local JSON + optional Supabase)
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator

import litellm as _litellm
import tiktoken

from src.core.config import get_settings
from src.core.exceptions import AllProvidersExhaustedError, SessionNotFoundError
from src.core.logger import log
from src.intelligence.cot import CoTHandler
from src.intelligence.emotion import detect_emotion, EmotionState
from src.intelligence.intent import IntentClassifier
from src.intelligence.language_profile import build_language_profile
from src.intelligence.persona import PersonaEngine
from src.intelligence.router import SmartRouter, get_router
from src.intelligence.self_model import get_self_model
from src.intelligence.telugu import detect_telugu, TeluguState
from src.intelligence.web_search import get_web_search as _get_web_search
from src.memory.embeddings import embed_one
from src.memory.rag import get_rag
from src.memory.supabase_client import get_supabase
from src.voice.state import get_voice_state_store

settings = get_settings()

# ── Token counting ────────────────────────────────────────────────────────────
_enc = tiktoken.get_encoding("cl100k_base")

def _count_tokens(text: str) -> int:
    return len(_enc.encode(text or ""))

def _messages_tokens(messages: list[dict]) -> int:
    return sum(_count_tokens(str(m.get("content", "") or "")) for m in messages)


# Phase 23: context-aware dynamic prompt pruning.
# Always keep the last N user/assistant pairs regardless of relevance score.
RECENCY_ANCHOR = 3

# _score_turns() relevance bonuses are plain substring checks against stored
# message text -- no extra metadata keys are added to message dicts (those
# get sent verbatim to LLM providers and extra fields can break strict schemas).
_REMEMBERED_FACT_MARKER = "[remembered fact]"
_MEMORY_ACK_MARKERS = ("i'll remember that:", "forgotten:")

# Embedding cache: text -> 384-dim vector. Avoids re-embedding the same
# user/assistant pair (or the same query) on every prune.
_EMBED_CACHE: dict[str, list[float]] = {}
_EMBED_CACHE_MAX = 2000


def _content_to_text(content: Any) -> str:
    """Flatten a message's `content` (str or multimodal list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts)
    return str(content or "")


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _cached_embed_one(text: str) -> list[float]:
    """embed_one() with a simple process-local cache keyed by exact text."""
    if text in _EMBED_CACHE:
        return _EMBED_CACHE[text]
    vec = embed_one(text)
    if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
        _EMBED_CACHE.clear()
    _EMBED_CACHE[text] = vec
    return vec


# ── Intent -> model category ───────────────────────────────────────────────────
_INTENT_TO_CATEGORY: dict[str, str] = {
    "chat":     "fast_chat",
    "casual":   "fast_chat",
    "academic": "academic",
    "medical":  "reasoning",
    "reasoning":"reasoning",
    "math":     "reasoning",
    "coding":   "coding",
    "code":     "coding",
    "vision":   "vision",
    "image":    "vision",
    "iot":      "fast_chat",
    "system":   "fast_chat",
    "cad":      "coding",
    "research":  "long_context",
    "creative":  "fast_chat",
    "computer":  "fast_chat",   # Phase 4: computer control intent
}

# ── Per-intent preferred model (Phase 3) ──────────────────────────────────────
# These override the category chain's default primary — the router injects the
# preferred model at position 0 before falling back to the rest of the chain.
# Only models whose provider key is in settings.available_providers will be used.
_INTENT_PREFERRED_MODELS: dict[str, str] = {
    # General conversation → Fireworks DeepSeek V3 (fast, capable, uses Fireworks credit)
    "chat":     "groq/qwen-2.5-32b",
    "casual":   "groq/qwen-2.5-32b",
    # Heavy reasoning
    "math":     "together_ai/deepseek-ai/DeepSeek-R1",
    "reasoning":"together_ai/deepseek-ai/DeepSeek-R1",
    "medical":  "together_ai/deepseek-ai/DeepSeek-R1",
    "academic": "together_ai/deepseek-ai/DeepSeek-R1",
    # Code
    "coding":   "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",
    "code":     "together_ai/Qwen/Qwen2.5-Coder-32B-Instruct",
    # Vision / multimodal
    "vision":   "gemini/gemini-2.0-flash",
    "image":    "gemini/gemini-2.0-flash",
    # Research & creative
    "research": "openrouter/google/gemma-3-27b-it:free",
    "creative": "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo",
}

MAX_CONTEXT_TOKENS = 8000

# ── Web-search trigger logic ───────────────────────────────────────────────────
import re as _re

_WEB_SEARCH_INTENTS: frozenset = frozenset({"research", "academic"})

# ── Trivial message detector (route to cheapest/fastest model) ────────────────
# Matches basic greetings, acks, and one-word replies that need zero reasoning.
_TRIVIAL_RE = _re.compile(
    r"^\s*(hi+|hey+|hello+|howdy|hiya|heya|sup|what'?s\s+up|whatsup|yo+|hola|namaste|"
    r"vanakkam|namaskar|salaam|"
    r"thanks?|thank\s+you|ty|thx|tnx|thks|tq|tysm|tyvm|"
    r"ok+|okay|kk|k|cool|sure|got\s+it|noted|roger|understood|"
    r"bye+|goodbye|good\s*bye|cya|see\s+ya|ttyl|later|"
    r"good\s+morning|gm|good\s+afternoon|good\s+evening|good\s+night|gn|"
    r"yes+|no+|yep|nope|nah|yeah+|yup|yea|"
    r"nice|great|awesome|perfect|good|wow|amazing|"
    r"lol|haha|hehe|lmao|😊|😄|👍|🙏)\s*[.!?🙂😊👍]*\s*$",
    _re.IGNORECASE,
)

# Most capable model for Tenglish / multilingual conversations
_TENGLISH_PREFERRED_MODEL  = "openrouter/deepseek/deepseek-r1:free"
_TENGLISH_FALLBACK_MODEL   = "together_ai/deepseek-ai/DeepSeek-R1"
_TENGLISH_CATEGORY         = "reasoning"

# Cheapest/fastest model for trivial English messages
_TRIVIAL_PREFERRED_MODEL   = "groq/llama-3.1-8b-instant"
_TRIVIAL_CATEGORY          = "fast_edge"


def _is_trivial_message(text: str) -> bool:
    """
    Returns True if the message is a basic greeting / ack / one-liner
    that requires no reasoning — safe to route to the lightest model.
    """
    stripped = text.strip()
    if not stripped:
        return False
    # Hard word-count gate: trivial messages are short
    if len(stripped.split()) > 6:
        return False
    return bool(_TRIVIAL_RE.match(stripped))

_LIVE_DATA_PATTERN = _re.compile(
    r"\b(latest|current|today|breaking\s+news|recent|"
    r"2024|2025|2026|"
    r"what\s+is\s+the\s+(current|latest|new)|who\s+is\s+(the\s+)?(current|new)|"
    r"price\s+of|weather\s+(in|at|for)|"
    r"stock\s+price|bitcoin|crypto|nft|"
    r"search\s+(for|the\s+web)|look\s+up|"
    r"news\s+(about|on)|"
    r"paper(s)?\s+on|journal|citation(s)?|academic|scholar|research\s+about|"
    r"what\s+happened|when\s+did|"
    r"is\s+it\s+true\s+that)\b",
    _re.IGNORECASE,
)


def _needs_web_search(intent: str, message: str) -> bool:
    """Return True if this request should trigger live web search."""
    return intent in _WEB_SEARCH_INTENTS or bool(_LIVE_DATA_PATTERN.search(message))


def _format_search_as_rag(search_data: dict) -> str:
    """Convert a search_result dict into a rag_context string for LLM injection."""
    synthesis = search_data.get("synthesis", "")
    provider  = search_data.get("provider", "web").upper()
    cites     = "\n".join(
        f"[{c['index']}] {c.get('title', '')} — {c.get('url', '')}"
        for c in search_data.get("citations", [])[:5]
    )
    return (
        f"[LIVE WEB SEARCH — {provider}]\n"
        f"{synthesis}\n\n"
        f"Sources:\n{cites}"
    )


def _normalize_search_payload(search_data: dict | None) -> dict | None:
    if not search_data:
        return None
    return {
        "used": bool(search_data.get("synthesis")),
        "provider": search_data.get("provider"),
        "queries": search_data.get("queries_used", []),
        "citations": search_data.get("citations", []),
        "latency_ms": search_data.get("latency_ms"),
        "model_used": search_data.get("model_used"),
    }


# ── Session store ─────────────────────────────────────────────────────────────
_SESSIONS_FILE = Path("memory/cache/sessions.json")
_SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_sessions() -> dict:
    if _SESSIONS_FILE.exists():
        try:
            return json.loads(_SESSIONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_sessions(sessions: dict) -> None:
    try:
        _SESSIONS_FILE.write_text(
            json.dumps(sessions, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning(f"[orchestrator] Could not persist sessions: {e}")


# ─────────────────────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(self, router: SmartRouter | None = None):
        self.router         = router or get_router()
        self.intent_clf     = IntentClassifier(router=self.router)
        self.persona_engine = PersonaEngine()
        self.cot_handler    = CoTHandler()
        self.rag            = get_rag()
        self.supabase       = get_supabase()
        self.voice_state    = get_voice_state_store()
        self.self_model     = get_self_model()
        self._sessions: dict[str, dict] = _load_sessions()
        log.info("[orchestrator] v7 ready  (intent + emotion + Telugu + persona + CoT + RAG + web-search + computer-control)")

    # ── Session helpers ────────────────────────────────────────────────────────

    def create_session(self, persona: str = "jarvis", user_id: str | None = None) -> str:
        sid = str(uuid.uuid4())
        self._sessions[sid] = {
            "id": sid,
            "created_at": time.time(),
            "persona": persona,
            "messages": [],
            "metadata": {},
            "title": None,
            "user_id": user_id,
        }
        _save_sessions(self._sessions)
        log.info(f"[orchestrator] New session {sid[:8]}... (persona={persona}, user={(user_id or '?')[:8]})")
        return sid

    def get_session(self, session_id: str) -> dict:
        if session_id not in self._sessions:
            raise SessionNotFoundError(f"Session '{session_id}' not found")
        return self._sessions[session_id]

    def session_owner(self, session_id: str) -> str | None:
        """Returns the owning user_id of a session, or None if unowned/unknown."""
        s = self._sessions.get(session_id)
        return s.get("user_id") if s else None

    def claim_session(self, session_id: str, user_id: str) -> None:
        """Assigns an owner to a legacy session that has no user_id yet."""
        s = self._sessions.get(session_id)
        if s is not None and s.get("user_id") is None and user_id:
            s["user_id"] = user_id
            _save_sessions(self._sessions)

    def list_sessions(self, user_id: str | None = None) -> list[dict]:
        results = []
        for s in self._sessions.values():
            # Skip internal / system sessions (e.g. episodic extraction)
            if s.get("id", "").startswith("__"):
                continue
            # Scope to a single user unless explicitly listing all (admin)
            if user_id is not None and s.get("user_id") not in (None, user_id):
                continue
            msgs = s.get("messages", [])
            preview = ""
            for m in msgs:
                if m.get("role") == "user":
                    preview = m.get("content", "")[:80]
                    break
            title = s.get("title") or preview or "New Chat"
            results.append({
                "id":            s["id"],
                "created_at":    s["created_at"],
                "updated_at":    s.get("updated_at", s["created_at"]),
                "persona":       s.get("persona", "jarvis"),
                "message_count": len(msgs),
                "preview":       preview,
                "title":         title,
                "user_id":       s.get("user_id"),
            })
        results.sort(key=lambda x: x["updated_at"], reverse=True)
        return results

    def list_users(self) -> list[dict]:
        """Admin helper: distinct user_ids with session counts / last activity."""
        users: dict[str, dict] = {}
        for s in self._sessions.values():
            if s.get("id", "").startswith("__"):
                continue
            uid = s.get("user_id") or "unclaimed"
            updated = s.get("updated_at", s.get("created_at", 0))
            u = users.setdefault(uid, {"user_id": uid, "session_count": 0, "last_active": 0})
            u["session_count"] += 1
            u["last_active"] = max(u["last_active"], updated)
        return sorted(users.values(), key=lambda x: x["last_active"], reverse=True)

    def clear_session(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id]["messages"] = []
            _save_sessions(self._sessions)

    def delete_session(self, session_id: str) -> bool:
        """Permanently removes a session from the store."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            _save_sessions(self._sessions)
            log.info(f"[orchestrator] Session {session_id[:8]}... deleted")
            return True
        return False

    def rename_session(self, session_id: str, title: str) -> None:
        """Sets a human-readable title for a session."""
        if session_id in self._sessions:
            self._sessions[session_id]["title"] = title.strip()[:80]
            _save_sessions(self._sessions)

    def set_persona(self, session_id: str, persona: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id]["persona"] = persona
            _save_sessions(self._sessions)

    # ── Context window management ──────────────────────────────────────────────

    def _score_turns(self, messages: list[dict], current_query: str) -> list[float]:
        """
        messages[0] = system prompt (never scored/dropped)
        messages[1:] = alternating user/assistant turns (paired)

        Returns one relevance score per (user, assistant) PAIR, in order:
          score = cosine_similarity(pair_text, current_query)
            +0.5 if the pair contains a "[Remembered fact]" marker
            +0.3 if the pair is a remember/forget command + acknowledgement

        If the embedding model is unavailable, cosine similarity contributes
        0.0 for every pair but the marker bonuses above still apply (plain
        substring checks, no embeddings needed) -- so remembered facts and
        remember/forget exchanges are still preferentially retained.
        """
        history = messages[1:]
        n_pairs = len(history) // 2
        if n_pairs == 0:
            return []
        if not current_query:
            return [0.0] * n_pairs

        try:
            query_vec = _cached_embed_one(current_query)
        except Exception as e:
            log.debug(f"[orchestrator] _score_turns query embedding failed (non-fatal): {e}")
            query_vec = None

        scores: list[float] = []
        for i in range(0, n_pairs * 2, 2):
            user_msg, asst_msg = history[i], history[i + 1]
            user_text = _content_to_text(user_msg.get("content", ""))
            asst_text = _content_to_text(asst_msg.get("content", ""))
            pair_text = f"{user_text}\n{asst_text}".strip()

            score = 0.0
            if query_vec is not None:
                try:
                    score = _cosine_sim(_cached_embed_one(pair_text), query_vec)
                except Exception:
                    score = 0.0

            pair_lower = pair_text.lower()
            if _REMEMBERED_FACT_MARKER in pair_lower:
                score += 0.5
            if any(marker in pair_lower for marker in _MEMORY_ACK_MARKERS):
                score += 0.3

            scores.append(score)
        return scores

    def _trim_history(self, messages: list[dict], current_query: str = "") -> list[dict]:
        """
        Trim `messages` to fit MAX_CONTEXT_TOKENS.

        - PRUNING_STRATEGY == "fifo" (or current_query == ""): drop the oldest
          user/assistant pairs first -- original behavior, unchanged.
        - PRUNING_STRATEGY == "semantic" (default): always keep the system
          prompt and the last RECENCY_ANCHOR pairs; among the remaining
          (older) pairs, drop the lowest-scoring ones first (per
          _score_turns) until under budget. Result stays in chronological
          order (pairs are never reordered, only dropped).

        Note: no `role == "tool"` messages currently exist anywhere in the
        session message structure (verified -- only plain user/assistant
        pairs are ever appended), so the tool-call-pairing edge case does
        not apply.
        """
        if _messages_tokens(messages) <= MAX_CONTEXT_TOKENS:
            return messages

        strategy = getattr(settings, "pruning_strategy", "semantic")

        if strategy == "fifo" or not current_query:
            # Original FIFO behavior -- unchanged (fallback / backward compat).
            while _messages_tokens(messages) > MAX_CONTEXT_TOKENS and len(messages) > 2:
                messages.pop(1)
                if len(messages) > 1:
                    messages.pop(1)
            return messages

        if len(messages) <= 2:
            return messages

        system_msg  = messages[0]
        current_msg = messages[-1]
        history     = messages[1:-1]

        # Pair up history into (user, assistant) turns. Defensive: an
        # unpaired trailing message becomes its own single-element "pair"
        # so it's never silently dropped or duplicated.
        pairs: list[list[dict]] = []
        i = 0
        while i < len(history):
            if i + 1 < len(history):
                pairs.append([history[i], history[i + 1]])
                i += 2
            else:
                pairs.append([history[i]])
                i += 1

        n_pairs = len(pairs)
        if n_pairs <= RECENCY_ANCHOR:
            # Too little history to prune -- keep everything (edge case).
            return messages

        anchor_pairs    = pairs[-RECENCY_ANCHOR:]
        scoreable_pairs = pairs[:-RECENCY_ANCHOR]

        flat_scoreable = [m for pair in scoreable_pairs for m in pair]
        scores = self._score_turns([system_msg, *flat_scoreable], current_query)
        if len(scores) != len(scoreable_pairs):
            # Misaligned (shouldn't happen for normal user/assistant
            # history) -- fall back to neutral scores (= oldest-first drop).
            scores = [0.0] * len(scoreable_pairs)

        keep = [True] * len(scoreable_pairs)

        def _assemble() -> list[dict]:
            out = [system_msg]
            for idx, pair in enumerate(scoreable_pairs):
                if keep[idx]:
                    out.extend(pair)
            for pair in anchor_pairs:
                out.extend(pair)
            out.append(current_msg)
            return out

        result = _assemble()
        # Drop lowest-scoring pairs first until under budget (or nothing left to drop).
        for idx in sorted(range(len(scoreable_pairs)), key=lambda j: scores[j]):
            if _messages_tokens(result) <= MAX_CONTEXT_TOKENS:
                break
            keep[idx] = False
            result = _assemble()

        return result

    # ── Build final message array ──────────────────────────────────────────────

    def _build_messages(
        self,
        history: list[dict],
        user_content: str | list,
        system_prompt: str,
    ) -> list[dict]:
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})
        current_query = _content_to_text(user_content)
        return self._trim_history(messages, current_query=current_query)

    async def _build_messages_async(
        self,
        history: list[dict],
        user_content: str | list,
        system_prompt: str,
    ) -> list[dict]:
        """Async version of _build_messages that doesn't block the event loop.

        The embedding operations in _trim_history are CPU-intensive and synchronous.
        This wrapper runs them in a thread pool to prevent blocking the event loop.
        """
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_content})
        current_query = _content_to_text(user_content)

        # Run blocking _trim_history in a thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,  # Use default ThreadPoolExecutor
            self._trim_history,
            messages,
            current_query
        )

    # ── Main chat pipeline ─────────────────────────────────────────────────────

    async def chat(
        self,
        user_message: str,
        session_id: str | None = None,
        intent: str = "auto",
        persona: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
        rag_context: str | None = None,
        image_data: list | None = None,
        extra_system: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """
        Full pipeline: intent -> persona -> CoT -> route -> post-process.
        """
        _is_internal = bool(session_id) and str(session_id).startswith("__")

        # 1. Session — internal calls get an ephemeral in-memory session (never saved)
        if _is_internal:
            # Use the __ep_... ID directly as a throwaway dict; discarded after this call.
            self._sessions[session_id] = {
                "id": session_id, "created_at": time.time(),
                "persona": persona or "jarvis", "messages": [], "metadata": {}, "title": None,
                "user_id": user_id,
            }
        elif not session_id or session_id not in self._sessions:
            session_id = self.create_session(persona=persona or "jarvis", user_id=user_id)
        session = self._sessions[session_id]
        if session.get("user_id") is None and user_id:
            session["user_id"] = user_id
        active_persona = persona or session.get("persona", "jarvis")

        # 1b. Action commands (e.g. WhatsApp send) — act instead of chatting
        from src.intelligence.commands import try_handle_command
        _cmd_reply = None
        if not _is_internal and intent in ("auto", "chat"):
            _cmd_reply = await try_handle_command(user_message, active_persona, session_id=session_id)
        if _cmd_reply is not None:
            session["messages"].append({"role": "user", "content": user_message})
            session["messages"].append({"role": "assistant", "content": _cmd_reply})
            session["updated_at"] = time.time()
            _save_sessions(self._sessions)
            # Run emotion/telugu even for command replies so HUD gets the signal
            _cmd_emotion = detect_emotion(user_message)
            _cmd_telugu  = detect_telugu(user_message)
            return {
                "reply": _cmd_reply, "model": "command", "session_id": session_id,
                "intent": "command", "persona": active_persona, "confidence": 1.0,
                "tokens_used": 0, "latency_ms": 0, "thinking": None, "error": None,
                "emotion": _cmd_emotion.primary.value,
                "emotion_intensity": _cmd_emotion.intensity,
                "telugu": _cmd_telugu.dialect if _cmd_telugu.detected else None,
                "language_profile": build_language_profile(_cmd_reply or user_message, telugu_state=_cmd_telugu),
                "voice_output_enabled": self.voice_state.get().get("voice_output_enabled", False),
                "academic_mode": classified_intent == "academic" if 'classified_intent' in locals() else False,
                "search": None,
                "self_model": self.self_model.get(),
            }

        # 2. Intent classification
        if image_data:
            classified_intent, confidence, clf_method = "vision", 1.0, "forced"
        elif intent == "auto" or intent == "chat":
            classified_intent, confidence, clf_method = await self.intent_clf.classify(
                user_message
            )
        else:
            classified_intent, confidence, clf_method = intent, 1.0, "explicit"

        # 2b. Emotion detection (zero-latency, pattern-based)
        emotion_state: EmotionState = detect_emotion(user_message)
        if not emotion_state.is_neutral:
            log.debug(
                f"[orchestrator] Emotion: {emotion_state.primary.value} "
                f"(intensity={emotion_state.intensity}, cues={emotion_state.cues[:3]})"
            )

        # 2c. Telugu language detection
        telugu_state: TeluguState = detect_telugu(user_message)
        if telugu_state.detected:
            log.info(
                f"[orchestrator] Telugu detected: dialect={telugu_state.dialect} "
                f"(conf={telugu_state.confidence:.0%})"
            )
        language_profile = build_language_profile(
            user_message,
            telugu_state=telugu_state,
        )
        self_model_state = self.self_model.reflect(
            user_message=user_message,
            intent=classified_intent,
            emotion=emotion_state.primary.value,
            language_profile=language_profile,
        )
        self.voice_state.set_language_profile(language_profile, persona=active_persona)

        # 3. RAG retrieval
        if rag_context is None:
            should_rag = await self.rag.should_use_rag(user_message, classified_intent)
            if should_rag:
                rag_context, _ = await self.rag.retrieve(query=user_message)
                if rag_context:
                    log.debug("[orchestrator] RAG context injected")

        # 3a. Always-on long-term memory check ("memory mode") — regardless of
        #     intent or should_use_rag(), surface any facts the user explicitly
        #     told TRON-X to remember (stored via "remember ..." commands).
        #     This is what makes "remember X" recall work in brand-new sessions.
        if not _is_internal:
            try:
                know_ctx, _know_hits = await self.rag.retrieve_knowledge(user_message)
                if know_ctx:
                    rag_context = f"{rag_context}\n\n{know_ctx}" if rag_context else know_ctx
                    log.debug("[orchestrator] Remembered-fact context injected")
            except Exception as e:
                log.debug(f"[orchestrator] Knowledge recall failed (non-fatal): {e}")

        # 3b. Web search (research intent + live-data queries) — non-streaming path
        _search_result: dict | None = None
        if _needs_web_search(classified_intent, user_message):
            try:
                _ws = _get_web_search()
                _sr = await _ws.search(
                    query=user_message,
                    intent=classified_intent,
                    persona=active_persona,
                    emotion_state=emotion_state,
                    telugu_state=telugu_state,
                    router=self.router,
                )
                if _sr.synthesis:
                    _search_result = {
                        "synthesis":    _sr.synthesis,
                        "citations":    [{"index": c.index, "title": c.title, "url": c.url,
                                          "snippet": c.snippet, "date": c.date}
                                         for c in _sr.citations],
                        "queries_used": _sr.queries_used,
                        "provider":     _sr.provider,
                        "model_used":   _sr.model_used,
                        "latency_ms":   _sr.latency_ms,
                    }
            except Exception as _se:
                log.warning(f"[orchestrator] Web search error (non-fatal): {_se}")

        # 3c. Research intent + successful search → return synthesis directly (skip main LLM)
        if classified_intent == "research" and _search_result and _search_result.get("synthesis"):
            _synth  = _search_result["synthesis"]
            _final  = self.persona_engine.sanitize_response(_synth, active_persona)
            _t0_tmp = time.monotonic()
            if not _is_internal:
                if not session.get("title") and not session.get("messages"):
                    session["title"] = user_message[:60].strip()
                session["messages"].append({"role": "user",      "content": user_message})
                session["messages"].append({"role": "assistant", "content": _final})
                session["updated_at"] = time.time()
                _save_sessions(self._sessions)
                try:
                    await self.rag.store_turn(session_id=session_id, user_msg=user_message,
                                              assistant_msg=_final, intent=classified_intent)
                except Exception:
                    pass
            return {
                "reply":             _final,
                "model":             _search_result.get("model_used", "search"),
                "session_id":        session_id,
                "intent":            classified_intent,
                "persona":           active_persona,
                "confidence":        confidence,
                "tokens_used":       len(_final.split()),
                "latency_ms":        _search_result.get("latency_ms", 0),
                "thinking":          None,
                "emotion":           emotion_state.primary.value,
                "emotion_intensity": emotion_state.intensity,
                "telugu":            telugu_state.dialect if telugu_state.detected else None,
                "search_used":       True,
                "citations":         _search_result.get("citations", []),
                "search_queries":    _search_result.get("queries_used", []),
                "search":            _normalize_search_payload(_search_result),
                "language_profile":  language_profile,
                "voice_output_enabled": self.voice_state.get().get("voice_output_enabled", False),
                "academic_mode":     classified_intent == "academic",
                "self_model":        self_model_state,
                "error":             None,
            }

        # Inject live search result as RAG context for other intents (weather, stocks, news, etc.)
        if _search_result and _search_result.get("synthesis") and rag_context is None:
            rag_context = _format_search_as_rag(_search_result)

        # 4. Build system prompt (now includes emotion + Telugu awareness)
        system_prompt = self.persona_engine.build_system_prompt(
            intent=classified_intent,
            persona=active_persona,
            rag_context=rag_context,
            emotion_state=emotion_state,
            telugu_state=telugu_state,
        )
        if extra_system:
            system_prompt = extra_system + "\n\n" + system_prompt

        # 4a-bis. [Phase 38] Self-model injection — the persona speaks from
        # its real internal state (mood, uptime, recent focus). Never fatal.
        if settings.self_model_enabled and not _is_internal:
            try:
                system_prompt = system_prompt + "\n\n" + self.self_model.system_note()
            except Exception:
                pass

        # 4b. Chain-of-Thought injection
        if self.cot_handler.needs_cot(classified_intent):
            system_prompt = self.cot_handler.inject(system_prompt, classified_intent)

        # 5. Build multimodal content
        if image_data:
            user_content: Any = [
                {"type": "text", "text": user_message},
                *image_data,
            ]
        else:
            user_content = user_message

        # 6. Assemble messages
        category        = _INTENT_TO_CATEGORY.get(classified_intent, "fast_chat")
        preferred_model = _INTENT_PREFERRED_MODELS.get(classified_intent)

        # ── Telugu/Tenglish override → most capable model ─────────────────────
        # When user writes in Tenglish, Romanised Telugu, Telugu script, or Hyderabadi,
        # force the highest-capability model for nuanced multilingual understanding.
        if telugu_state.detected and telugu_state.requires_high_model:
            category        = _TENGLISH_CATEGORY
            preferred_model = _TENGLISH_PREFERRED_MODEL
            log.info(
                f"[orchestrator] Telugu override: dialect={telugu_state.dialect} "
                f"(conf={telugu_state.confidence:.0%}) → "
                f"category={_TENGLISH_CATEGORY}, model={_TENGLISH_PREFERRED_MODEL}"
            )
        # ── Trivial English override → cheapest/fastest model ─────────────────
        # Basic greetings and acks (hi, hello, thanks, ok) need no reasoning.
        elif not telugu_state.detected and _is_trivial_message(user_message):
            category        = _TRIVIAL_CATEGORY
            preferred_model = _TRIVIAL_PREFERRED_MODEL
            log.debug(
                f"[orchestrator] Trivial message detected → "
                f"category={_TRIVIAL_CATEGORY}, model={_TRIVIAL_PREFERRED_MODEL}"
            )

        messages        = self._build_messages(session["messages"], user_content, system_prompt)

        # 7. Route to LLM
        t0 = time.monotonic()
        thinking: str | None = None

        try:
            response, model_used = await self.router.complete(
                messages=messages,
                category=category,
                stream=False,
                preferred_model=preferred_model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except AllProvidersExhaustedError as e:
            log.error(f"[orchestrator] All providers failed: {e}")
            return {
                "reply": (
                    "All AI providers are currently unavailable. "
                    "Check your API keys in .env or try again shortly."
                ),
                "model": "none",
                "session_id": session_id,
                "intent": classified_intent,
                "persona": active_persona,
                "confidence": confidence,
                "tokens_used": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": int((time.monotonic() - t0) * 1000),
                "thinking": None,
                "error": str(e),
                "emotion": emotion_state.primary.value,
                "emotion_intensity": emotion_state.intensity,
                "telugu": telugu_state.dialect if telugu_state.detected else None,
                "language_profile": language_profile,
                "voice_output_enabled": self.voice_state.get().get("voice_output_enabled", False),
                "academic_mode": classified_intent == "academic",
                "search": _normalize_search_payload(_search_result),
                "self_model": self_model_state,
            }

        latency_ms = int((time.monotonic() - t0) * 1000)
        raw_reply = response.choices[0].message.content or ""
        tokens_used = getattr(response.usage, "total_tokens", 0)
        # [Phase 34] Split input/output tokens for per-model cost estimation.
        prompt_tokens = getattr(response.usage, "prompt_tokens", 0)
        completion_tokens = getattr(response.usage, "completion_tokens", 0)

        # 8. Post-process
        visible_reply, thinking = self.cot_handler.extract_thinking(raw_reply)
        final_reply = self.persona_engine.sanitize_response(visible_reply, active_persona)

        # 9. Persist conversation
        if _is_internal:
            # Internal calls are ephemeral — discard session immediately, persist nothing.
            self._sessions.pop(session_id, None)
        else:
            # Auto-title from first user message if no title set yet
            if not session.get("title") and not session.get("messages"):
                session["title"] = user_message[:60].strip()
            session["messages"].append({"role": "user", "content": user_message})
            session["messages"].append({"role": "assistant", "content": final_reply})
            session["updated_at"] = time.time()
            _save_sessions(self._sessions)

            try:
                await self.rag.store_turn(
                    session_id=session_id,
                    user_msg=user_message,
                    assistant_msg=final_reply,
                    intent=classified_intent,
                )
            except Exception as e:
                log.debug(f"[orchestrator] RAG store failed (non-fatal): {e}")

            try:
                await self.supabase.save_message(session_id, "user", user_message,
                                                 {"intent": classified_intent})
                await self.supabase.save_message(session_id, "assistant", final_reply,
                                                 {"model": model_used, "tokens": tokens_used})
            except Exception:
                pass

        log.info(
            f"[orchestrator] {session_id[:8]}... | "
            f"intent={classified_intent} ({confidence:.2f}) | "
            f"persona={active_persona} | model={model_used} | "
            f"tokens={tokens_used} | {latency_ms}ms"
        )

        return {
            "reply":       final_reply,
            "model":       model_used,
            "session_id":  session_id,
            "intent":      classified_intent,
            "persona":     active_persona,
            "confidence":  round(confidence, 3),
            "tokens_used": tokens_used,
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_ms":  latency_ms,
            "thinking":    thinking,
            "emotion":     emotion_state.primary.value,
            "emotion_intensity": emotion_state.intensity,
            "telugu":      telugu_state.dialect if telugu_state.detected else None,
            "language_profile": language_profile,
            "voice_output_enabled": self.voice_state.get().get("voice_output_enabled", False),
            "academic_mode": classified_intent == "academic",
            "search": _normalize_search_payload(_search_result),
            "citations": (_search_result or {}).get("citations", []),
            "self_model": self_model_state,
        }

    # ── Streaming chat pipeline ────────────────────────────────────────────────

    async def chat_stream(
        self,
        user_message: str,
        session_id: str | None = None,
        intent: str = "auto",
        persona: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        extra_system: str | None = None,
        image_data: list | None = None,
        user_id: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """
        Streaming pipeline — same setup as chat() but yields SSE dicts.

        Event types:
          meta  -> intent, session_id, persona
          text  -> content (one per chunk)
          done  -> model, latency_ms, tokens_used, session_id, intent, persona
          error -> message
        """
        _is_internal = bool(session_id) and str(session_id).startswith("__")

        # 1. Session — internal calls get an ephemeral in-memory session (never saved)
        if _is_internal:
            self._sessions[session_id] = {
                "id": session_id, "created_at": time.time(),
                "persona": persona or "jarvis", "messages": [], "metadata": {}, "title": None,
                "user_id": user_id,
            }
        elif not session_id or session_id not in self._sessions:
            session_id = self.create_session(persona=persona or "jarvis", user_id=user_id)
        session = self._sessions[session_id]
        if session.get("user_id") is None and user_id:
            session["user_id"] = user_id
        active_persona = persona or session.get("persona", "jarvis")

        # 1b. Action commands (e.g. WhatsApp send) — act instead of chatting
        from src.intelligence.commands import try_handle_command
        _cmd_reply = None
        if not _is_internal and intent in ("auto", "chat"):
            _cmd_reply = await try_handle_command(user_message, active_persona, session_id=session_id)
        if _cmd_reply is not None:
            _cmd_telugu = detect_telugu(user_message)
            _cmd_lang = build_language_profile(user_message, telugu_state=_cmd_telugu)
            _voice_state = self.voice_state.set_language_profile(_cmd_lang, persona=active_persona)
            yield {
                "type": "meta",
                "intent": "command",
                "session_id": session_id,
                "persona": active_persona,
                "language_profile": _cmd_lang,
                "voice_output_enabled": _voice_state.get("voice_output_enabled", False),
                "academic_mode": False,
                "search": None,
            }
            yield {"type": "text", "content": _cmd_reply}
            session["messages"].append({"role": "user", "content": user_message})
            session["messages"].append({"role": "assistant", "content": _cmd_reply})
            session["updated_at"] = time.time()
            _save_sessions(self._sessions)
            yield {"type": "done", "model": "command", "latency_ms": 0,
                   "tokens_used": 0, "session_id": session_id,
                   "intent": "command", "persona": active_persona,
                   "language_profile": _cmd_lang,
                   "voice_output_enabled": _voice_state.get("voice_output_enabled", False),
                   "academic_mode": False,
                   "search": None}
            return

        # 2. Intent classification
        classified_intent, confidence, _ = await self.intent_clf.classify(user_message)

        # 2b. Emotion + Telugu detection (zero-latency)
        emotion_state: EmotionState = detect_emotion(user_message)
        telugu_state:  TeluguState  = detect_telugu(user_message)
        language_profile = build_language_profile(
            user_message,
            telugu_state=telugu_state,
        )
        self_model_state = self.self_model.reflect(
            user_message=user_message,
            intent=classified_intent,
            emotion=emotion_state.primary.value,
            language_profile=language_profile,
        )
        voice_state = self.voice_state.set_language_profile(language_profile, persona=active_persona)

        log.info("[orchestrator/stream] Done meta detection, yielding meta event")

        yield {
            "type":       "meta",
            "intent":     classified_intent,
            "session_id": session_id,
            "persona":    active_persona,
            "emotion":    emotion_state.primary.value,
            "telugu":     telugu_state.dialect if telugu_state.detected else None,
            "language_profile": language_profile,
            "voice_output_enabled": voice_state.get("voice_output_enabled", False),
            "academic_mode": classified_intent == "academic",
            "search": None,
            "self_model": self_model_state,
        }

        t0 = time.monotonic()

        # 2c. Computer intent fast-path — hand off to VisualComputerAgent (Phase 4)
        if classified_intent == "computer":
            from src.agents.visual_computer import get_visual_computer
            _vc = get_visual_computer()
            async for _cv_ev in _vc.stream(
                instruction=user_message,
                persona=active_persona,
                router=self.router,
            ):
                yield _cv_ev
            # Persist a summary turn so the session has a record
            if not _is_internal:
                _cv_summary = f"[Computer task] {user_message}"
                if not session.get("title") and not session.get("messages"):
                    session["title"] = user_message[:60].strip()
                session["messages"].append({"role": "user",      "content": user_message})
                session["messages"].append({"role": "assistant", "content": _cv_summary})
                session["updated_at"] = time.time()
                _save_sessions(self._sessions)
            yield {
                "type":              "done",
                "model":             "computer_agent",
                "session_id":        session_id,
                "intent":            "computer",
                "persona":           active_persona,
                "latency_ms":        int((time.monotonic() - t0) * 1000),
                "tokens_used":       0,
                "emotion":           emotion_state.primary.value,
                "emotion_intensity": emotion_state.intensity,
                "telugu":            telugu_state.dialect if telugu_state.detected else None,
                "search_used":       False,
                "language_profile":  language_profile,
                "voice_output_enabled": voice_state.get("voice_output_enabled", False),
                "academic_mode":     False,
                "search":            None,
                "self_model":        self_model_state,
            }
            return

        # 3. Web search (research intent + live-data queries)
        _search_result: dict | None = None
        if _needs_web_search(classified_intent, user_message):
            _ws = _get_web_search()
            async for _ev in _ws.stream(
                query=user_message,
                intent=classified_intent,
                persona=active_persona,
                emotion_state=emotion_state,
                telugu_state=telugu_state,
                router=self.router,
            ):
                if _ev["type"] == "search_progress":
                    yield _ev           # forward progress events to HUD
                elif _ev["type"] == "search_result":
                    _search_result = _ev["data"]
                elif _ev["type"] == "error":
                    log.warning(f"[orchestrator/stream] Web search error: {_ev.get('message')}")

        # 3b. Research intent + successful synthesis -> stream answer directly, skip main LLM
        if classified_intent == "research" and _search_result and _search_result.get("synthesis"):
            _synth = _search_result["synthesis"]
            _words = _synth.split(" ")
            _n     = len(_words)
            for _i, _word in enumerate(_words):
                yield {"type": "text", "content": _word + (" " if _i < _n - 1 else "")}
            if not _is_internal:
                if not session.get("title") and not session.get("messages"):
                    session["title"] = user_message[:60].strip()
                session["messages"].append({"role": "user",      "content": user_message})
                session["messages"].append({"role": "assistant", "content": _synth})
                session["updated_at"] = time.time()
                _save_sessions(self._sessions)
                try:
                    await self.rag.store_turn(session_id=session_id, user_msg=user_message,
                                              assistant_msg=_synth, intent=classified_intent)
                except Exception:
                    pass
            yield {
                "type":             "done",
                "model":            _search_result.get("model_used", "search"),
                "session_id":       session_id,
                "intent":           classified_intent,
                "persona":          active_persona,
                "latency_ms":       _search_result.get("latency_ms", int((time.monotonic()-t0)*1000)),
                "tokens_used":      len(_synth.split()),
                "emotion":          emotion_state.primary.value,
                "emotion_intensity": emotion_state.intensity,
                "telugu":           telugu_state.dialect if telugu_state.detected else None,
                "search_used":      True,
                "citations":        _search_result.get("citations", []),
                "search_queries":   _search_result.get("queries_used", []),
                "language_profile": language_profile,
                "voice_output_enabled": voice_state.get("voice_output_enabled", False),
                "academic_mode":    classified_intent == "academic",
                "search":           _normalize_search_payload(_search_result),
                "self_model":       self_model_state,
            }
            return

        # 4. RAG retrieval (for non-research intents; inject live search results if available)
        log.info("[orchestrator/stream] Starting RAG retrieval")
        rag_context: str | None = None
        if _search_result and _search_result.get("synthesis"):
            rag_context = _format_search_as_rag(_search_result)
        else:
            should_rag = await self.rag.should_use_rag(user_message, classified_intent)
            if should_rag:
                rag_context, _ = await self.rag.retrieve(query=user_message)

        # 4a. Always-on long-term memory check ("memory mode") — same as in
        #     chat(): surface explicitly "remembered" facts in every session,
        #     regardless of intent or should_use_rag().
        log.info("[orchestrator/stream] Checking knowledge collection")
        if not _is_internal:
            try:
                know_ctx, _know_hits = await self.rag.retrieve_knowledge(user_message)
                if know_ctx:
                    rag_context = f"{rag_context}\n\n{know_ctx}" if rag_context else know_ctx
            except Exception as e:
                log.debug(f"[orchestrator/stream] Knowledge recall failed (non-fatal): {e}")

        # 5. System prompt (emotion + Telugu + RAG wired in)
        log.info("[orchestrator/stream] Building system prompt")
        system_prompt = self.persona_engine.build_system_prompt(
            intent=classified_intent,
            persona=active_persona,
            rag_context=rag_context,
            emotion_state=emotion_state,
            telugu_state=telugu_state,
        )
        if self.cot_handler.needs_cot(classified_intent):
            system_prompt = self.cot_handler.inject(system_prompt, classified_intent)

        if extra_system:
            system_prompt = extra_system + "\n\n" + system_prompt

        user_content = user_message
        if image_data:
            user_content = [{"type": "text", "text": user_message}]
            user_content.extend([{"type": "image_url", "image_url": {"url": img}} for img in image_data])

        # 6. Messages (using async version to prevent event loop blocking)
        category        = _INTENT_TO_CATEGORY.get(classified_intent, "fast_chat")
        preferred_model = _INTENT_PREFERRED_MODELS.get(classified_intent)

        # ── Telugu/Tenglish override → most capable model ─────────────────────
        if telugu_state.detected and telugu_state.requires_high_model:
            category        = _TENGLISH_CATEGORY
            preferred_model = _TENGLISH_PREFERRED_MODEL
            log.info(
                f"[orchestrator/stream] Telugu override: dialect={telugu_state.dialect} "
                f"(conf={telugu_state.confidence:.0%}) → "
                f"category={_TENGLISH_CATEGORY}, model={_TENGLISH_PREFERRED_MODEL}"
            )
        # ── Trivial English override → cheapest/fastest model ─────────────────
        elif not telugu_state.detected and _is_trivial_message(user_message):
            category        = _TRIVIAL_CATEGORY
            preferred_model = _TRIVIAL_PREFERRED_MODEL
            log.debug(
                f"[orchestrator/stream] Trivial message → "
                f"category={_TRIVIAL_CATEGORY}, model={_TRIVIAL_PREFERRED_MODEL}"
            )

        log.info("[orchestrator/stream] Awaiting _build_messages_async")
        messages        = await self._build_messages_async(session["messages"], user_content, system_prompt)
        log.info("[orchestrator/stream] Messages built successfully")

        # 7. Stream from best available model
        chain      = self.router._get_chain(category, preferred_model=preferred_model)
        ab_exp_id  = (
            self.router.ab_tests.experiment_for_category(category)
            if not preferred_model else None
        )
        ab_variant_models = set(
            v["model"]
            for v in self.router.ab_tests._experiments.get(ab_exp_id or "", {}).get("variants", [])
        )
        full_reply  = ""
        model_used  = "unknown"
        streamed_ok = False

        for model_id in chain:
            if not self.router.health.is_available(model_id):
                continue
            if self.router.rate_limiter.is_limited(model_id):
                continue
            try:
                log.info(f"[orchestrator/stream] -> {model_id}")
                filtered = self.router._filter_params(model_id, {
                    "temperature": temperature,
                    "max_tokens":  max_tokens,
                })
                self.router.rate_limiter.record(model_id)
                response = await _litellm.acompletion(
                    model=model_id,
                    messages=messages,
                    stream=True,
                    timeout=60,
                    **filtered,
                )
                model_used = model_id
                async for chunk in response:
                    delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                    if delta:
                        full_reply += delta
                        yield {"type": "text", "content": delta}
                stream_ms = (time.monotonic() - t0) * 1000
                self.router.health.mark_success(model_id)
                self.router.latency_tracker.record(model_id, stream_ms)
                if ab_exp_id and model_id in ab_variant_models:
                    self.router.ab_tests.record(ab_exp_id, model_id, stream_ms,
                                                len(full_reply.split()), True)
                streamed_ok = True
                break
            except Exception as e:
                log.warning(f"[orchestrator/stream] {model_id} failed: {type(e).__name__}: {e}")
                self.router.health.mark_failure(model_id)
                if ab_exp_id and model_id in ab_variant_models:
                    self.router.ab_tests.record(ab_exp_id, model_id, 0, 0, False)
                continue

        if not streamed_ok:
            yield {"type": "error", "message": "All providers unavailable. Check API keys."}
            return

        # 8. Post-process + persist
        visible_reply, _ = self.cot_handler.extract_thinking(full_reply)
        final_reply = self.persona_engine.sanitize_response(visible_reply, active_persona)

        if _is_internal:
            self._sessions.pop(session_id, None)
        else:
            if not session.get("title") and not session.get("messages"):
                session["title"] = user_message[:60].strip()
            session["messages"].append({"role": "user",      "content": user_message})
            session["messages"].append({"role": "assistant", "content": final_reply})
            session["updated_at"] = time.time()
            _save_sessions(self._sessions)
            try:
                await self.rag.store_turn(
                    session_id=session_id,
                    user_msg=user_message,
                    assistant_msg=final_reply,
                    intent=classified_intent,
                )
            except Exception as e:
                log.debug(f"[orchestrator/stream] RAG store failed (non-fatal): {e}")

        yield {
            "type": "done",
            "reply": final_reply,
            "model": model_used,
            "session_id": session_id,
            "intent": classified_intent,
            "persona": active_persona,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "tokens_used": len(final_reply.split()),
            "emotion": emotion_state.primary.value,
            "emotion_intensity": emotion_state.intensity,
            "telugu": telugu_state.dialect if telugu_state.detected else None,
            "language_profile": language_profile,
            "voice_output_enabled": voice_state.get("voice_output_enabled", False),
            "academic_mode": classified_intent == "academic",
            "search": _normalize_search_payload(_search_result),
            "citations": (_search_result or {}).get("citations", []),
            "search_queries": (_search_result or {}).get("queries_used", []),
            "self_model": self_model_state,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────
_orchestrator_instance: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    """Get or create the global Orchestrator instance."""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = Orchestrator()
    return _orchestrator_instance
