"""
TRON-X  A.V.E.N.G.E.R.S  API
============================
REST:
  GET  /api/avengers/roster   -- the 21-persona roster (UI bootstrap)
  GET  /api/avengers/status   -- live dispatcher / voice / log status

WebSocket:  /ws/avengers
  client -> server
    {"type": "command",  "text": str, "session_id"?: str, "persona"?: str,
     "speak"?: bool, "agent_mode"?: bool}
    {"type": "audio",    "b64": str, "format": "webm"|"wav", "final": bool,
     "session_id"?: str, "wake_armed"?: bool, "speak"?: bool,
     "agent_mode"?: bool, "persona"?: str}
    {"type": "agent_stop"}                      (cancel an in-flight agent
                                                 chain before its next step)
    {"type": "subscribe_logs"}
    {"type": "ping"}

  server -> client
    {"type": "boot",        "roster": [...], "session_id": str, "wake_word": str}
    {"type": "agent_state", "id": str, "state": "active"|"idle"|"error"}
    {"type": "ops",         "persona": str, "summary": str, "data": any}
    {"type": "meta", ...}                       (orchestrator passthrough --
                                                 includes language_profile,
                                                 emotion, telugu when present)
    {"type": "text",        "content": str}     (streaming tokens)
    {"type": "done", ...}
    {"type": "agent_step",  "status": "next"|"complete"|"stopped"|"max_steps",
     "persona"?: str, "instruction"?: str, "step"?: int}
                                                 (agent-mode progress)
    {"type": "agent_strip", "text": str}        (the trailing ###NEXT...###
                                                 marker text -- frontend
                                                 removes it from the console)
    {"type": "transcript",  "text": str, "language": str}
    {"type": "wake",        "detected": bool, "transcript": str}
    {"type": "audio",       "b64": str, "format": str, "persona": str,
     "sentence": str}                            (Kokoro audio buffer push)
    {"type": "log",         "line": str}
    {"type": "error",       "message": str}
    {"type": "pong"}

Audio pipeline:
  browser VAD-gated chunks -> STT (Groq Whisper -> local fallback) ->
  server-side wake-word confirmation on the transcript -> dispatcher ->
  orchestrator token stream -> sentence buffer -> existing TTSEngine
  (Kokoro-ONNX local first) -> base64 audio frames pushed over the socket
  while the text streams into the terminal UI.

Voice output toggle:
  The client controls whether the server synthesizes TTS audio at all via
  the per-message "speak" flag (default true). When the Avengers HUD's
  "VOICE OUT" toggle is off, it sends "speak": false and only the text
  stream ("text"/"meta"/"done") is produced -- no "audio" frames are sent.

Agent mode (autonomous multi-step execution):
  When "agent_mode": true, the dispatcher appends a continuation-marker
  instruction to the persona's system prompt (see
  src/avengers/dispatcher.py: _agent_mode_instructions). After each "done"
  event, the server regex-parses the accumulated reply for a trailing
  "###NEXT persona=<id> :: <instruction>###" or "###NEXT done###" marker.
  If a persona+instruction marker is found, the server loops
  dispatch_stream() again with that persona and instruction (up to
  _AGENT_MAX_STEPS total steps), emitting "agent_step" events for UI
  progress and "agent_strip" so the marker text is hidden from the console.
  "agent_stop" cancels the chain before its next step begins.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.core.config import get_settings
from src.core.logger import log
from src.avengers.registry import get_roster, AVENGERS
from src.avengers.dispatcher import get_dispatcher

router = APIRouter()
settings = get_settings()

_LOG_PATH = Path("logs/tron_x.log")
_WAKE_WORDS = ("jarvis", "javis", "friday", "tron")
_SENTENCE_END = re.compile(r"([.!?。?!]+[\s\"')\]]*|\n+)")
_MIN_TTS_CHARS = 24      # don't synth ultra-short fragments (Kokoro guard)
_MAX_TTS_CHARS = 400     # split very long sentences

# Agent mode: max number of dispatch_stream() steps in one autonomous chain
# (the user's original command counts as step 1).
_AGENT_MAX_STEPS = 5

# Matches the trailing continuation marker emitted by an agent-mode reply:
#   ###NEXT persona=<id> :: <instruction>###
#   ###NEXT done###
_AGENT_NEXT_RE = re.compile(
    r"###NEXT\s+(?:done(?P<done_marker>)|persona\s*=\s*(?P<persona>[a-zA-Z_]+)\s*::\s*(?P<instruction>.*?))\s*###",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

@router.get("/api/avengers/roster")
async def avengers_roster() -> dict:
    return {"protocol": "JARVIS A.V.E.N.G.E.R.S", "count": 21, "roster": get_roster()}


@router.get("/api/avengers/status")
async def avengers_status() -> dict:
    from src.voice.tts import get_tts
    tts = get_tts()
    return {
        "dispatcher": "online",
        "personas": 21,
        "kokoro": bool(getattr(tts, "_kokoro_available", False)),
        "elevenlabs": bool(getattr(tts, "_el_available", False)),
        "log_file": str(_LOG_PATH),
        "wake_words": list(_WAKE_WORDS),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    try:
        await ws.send_text(json.dumps(payload, default=str))
        return True
    except Exception:
        return False


def _wake_in(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _WAKE_WORDS)


def _strip_wake(text: str) -> str:
    out = text
    for w in _WAKE_WORDS:
        out = re.sub(rf"\b(?:hey|okay|ok|yo)?\s*,?\s*{w}\b[,.!?]?\s*", " ", out, flags=re.I)
    return out.strip() or text.strip()


class _SentenceBuffer:
    """Accumulates streaming tokens and emits speakable sentences."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, chunk: str) -> list[str]:
        self._buf += chunk
        out: list[str] = []
        while True:
            m = _SENTENCE_END.search(self._buf)
            if not m:
                break
            end = m.end()
            sentence = self._buf[:end].strip()
            self._buf = self._buf[end:]
            if sentence:
                out.append(sentence)
        # Force-split runaway buffers (no punctuation for a long time)
        if len(self._buf) > _MAX_TTS_CHARS:
            out.append(self._buf.strip())
            self._buf = ""
        # Merge tiny fragments forward
        merged: list[str] = []
        for s in out:
            if merged and len(merged[-1]) < _MIN_TTS_CHARS:
                merged[-1] = f"{merged[-1]} {s}"
            else:
                merged.append(s)
        return merged

    def flush(self) -> str:
        rest, self._buf = self._buf.strip(), ""
        return rest


async def _speak(ws: WebSocket, text: str, persona: str, lang_hint: Optional[str]) -> None:
    """Synthesize one sentence via the existing TTS engine and push it."""
    if not text or len(text.strip()) < 2:
        return
    try:
        from src.voice.tts import get_tts
        res = await get_tts().synthesize(text, persona=persona, lang_hint=lang_hint)
        audio = res.get("audio_bytes", b"")
        if audio:
            await _safe_send(ws, {
                "type": "audio",
                "b64": base64.b64encode(audio).decode("ascii"),
                "format": res.get("format", "wav"),
                "provider": res.get("provider", "?"),
                "persona": persona,
                "sentence": text[:120],
            })
    except Exception as e:
        log.warning("[avengers-ws] TTS push failed: %s", e)


async def _tail_logs(ws: WebSocket) -> None:
    """Send the last 40 log lines then follow the file (1 Hz poll)."""
    try:
        last_size = 0
        if _LOG_PATH.exists():
            text = await asyncio.to_thread(
                _LOG_PATH.read_text, encoding="utf-8", errors="replace")
            last_size = _LOG_PATH.stat().st_size
            for line in text.splitlines()[-40:]:
                if not await _safe_send(ws, {"type": "log", "line": line}):
                    return
        while True:
            await asyncio.sleep(1.0)
            if not _LOG_PATH.exists():
                continue
            size = _LOG_PATH.stat().st_size
            if size < last_size:          # rotated
                last_size = 0
            if size > last_size:
                def _read_new() -> str:
                    with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(last_size)
                        return f.read()
                new = await asyncio.to_thread(_read_new)
                last_size = size
                for line in new.splitlines():
                    if line.strip() and not await _safe_send(ws, {"type": "log", "line": line}):
                        return
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug("[avengers-ws] log tail ended: %s", e)


# ---------------------------------------------------------------------------
# Command execution (shared by text + voice paths)
# ---------------------------------------------------------------------------

async def _run_command(
    ws: WebSocket,
    text: str,
    session_id: str,
    persona_override: Optional[str],
    speak: bool,
    agent_mode: bool = False,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    dispatcher = get_dispatcher()
    sentence_buf = _SentenceBuffer()
    lang_hint: Optional[str] = None
    speak_persona = "jarvis"
    tts_tasks: list[asyncio.Task] = []
    voice_queue: asyncio.Queue[Optional[tuple[str, str, Optional[str]]]] = asyncio.Queue()

    async def _voice_worker() -> None:
        """Serialize TTS so sentences arrive in order."""
        while True:
            item = await voice_queue.get()
            if item is None:
                return
            sentence, persona, hint = item
            await _speak(ws, sentence, persona, hint)

    worker = asyncio.create_task(_voice_worker()) if speak else None

    if stop_event is not None:
        stop_event.clear()

    try:
        current_text = text
        current_persona_override = persona_override

        for step in range(1, _AGENT_MAX_STEPS + 1):
            full_text_parts: list[str] = []

            async for event in dispatcher.dispatch_stream(
                    current_text, session_id=session_id,
                    persona_override=current_persona_override,
                    agent_mode=agent_mode):
                etype = event.get("type")
                if etype == "meta":
                    profile = event.get("language_profile") or {}
                    if isinstance(profile, dict):
                        # Matches src/intelligence/language_profile.py output —
                        # preserves Telugu TTS routing ("te" / "en-IN" / "en").
                        lang_hint = profile.get("preferred_tts_lang") or lang_hint
                    # Prefer the real persona id ("avenger") so each of the 21
                    # A.V.E.N.G.E.R.S personas gets its own voice profile —
                    # fall back to the collapsed jarvis/friday "persona" field,
                    # then to whatever we last resolved.
                    speak_persona = event.get("avenger") or event.get("persona") or speak_persona
                    if speak_persona not in AVENGERS:
                        speak_persona = "jarvis"
                if etype == "text":
                    full_text_parts.append(event.get("content", ""))
                if not await _safe_send(ws, event):
                    return
                if speak and etype == "text":
                    for sentence in sentence_buf.feed(event.get("content", "")):
                        voice_queue.put_nowait((sentence, speak_persona, lang_hint))
                if etype == "done" and speak:
                    rest = sentence_buf.flush()
                    if rest:
                        voice_queue.put_nowait((rest, speak_persona, lang_hint))

            if not agent_mode:
                return

            full_text = "".join(full_text_parts).rstrip()
            m = _AGENT_NEXT_RE.search(full_text)
            if not m or m.end() != len(full_text):
                # No (well-formed, trailing) continuation marker -> the model
                # didn't request another step. End the chain normally.
                return

            marker_text = full_text[m.start():m.end()]
            await _safe_send(ws, {"type": "agent_strip", "text": marker_text})

            if m.group("done_marker") is not None:
                await _safe_send(ws, {"type": "agent_step", "status": "complete", "step": step})
                return

            next_persona = (m.group("persona") or "").strip().lower()
            instruction = (m.group("instruction") or "").strip()

            if next_persona not in AVENGERS or not instruction:
                await _safe_send(ws, {"type": "agent_step", "status": "complete", "step": step})
                return

            if stop_event is not None and stop_event.is_set():
                await _safe_send(ws, {"type": "agent_step", "status": "stopped", "step": step})
                return

            if step >= _AGENT_MAX_STEPS:
                await _safe_send(ws, {"type": "agent_step", "status": "max_steps", "step": step})
                return

            await _safe_send(ws, {
                "type": "agent_step", "status": "next", "step": step + 1,
                "persona": next_persona, "instruction": instruction,
            })
            current_text = instruction
            current_persona_override = next_persona
    except Exception as e:
        log.error("[avengers-ws] command failed: %s", e)
        await _safe_send(ws, {"type": "error", "message": str(e)})
    finally:
        if worker is not None:
            voice_queue.put_nowait(None)
            try:
                await asyncio.wait_for(worker, timeout=90)
            except asyncio.TimeoutError:
                worker.cancel()
        for t in tts_tasks:
            t.cancel()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/avengers")
async def avengers_ws(ws: WebSocket) -> None:
    await ws.accept()
    session_id = f"avengers_{uuid.uuid4().hex[:10]}"
    log_task: Optional[asyncio.Task] = None
    audio_chunks: list[bytes] = []
    audio_format = "webm"
    agent_stop_event = asyncio.Event()

    await _safe_send(ws, {
        "type": "boot",
        "session_id": session_id,
        "wake_word": _WAKE_WORDS[0],
        "roster": get_roster(),
    })

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _safe_send(ws, {"type": "error", "message": "invalid JSON frame"})
                continue
            mtype = msg.get("type")

            if mtype == "ping":
                await _safe_send(ws, {"type": "pong", "t": time.time()})

            elif mtype == "subscribe_logs":
                if log_task is None or log_task.done():
                    log_task = asyncio.create_task(_tail_logs(ws))

            elif mtype == "agent_stop":
                agent_stop_event.set()

            elif mtype == "command":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                sid = msg.get("session_id") or session_id
                await _run_command(
                    ws, text, sid,
                    persona_override=msg.get("persona"),
                    speak=bool(msg.get("speak", True)),
                    agent_mode=bool(msg.get("agent_mode", False)),
                    stop_event=agent_stop_event,
                )

            elif mtype == "audio":
                b64 = msg.get("b64") or ""
                if b64:
                    try:
                        audio_chunks.append(base64.b64decode(b64))
                    except Exception:
                        await _safe_send(ws, {"type": "error", "message": "bad audio b64"})
                        continue
                audio_format = msg.get("format", audio_format)
                if not msg.get("final"):
                    continue

                blob, audio_chunks = b"".join(audio_chunks), []
                if len(blob) < 1200:   # too short to be speech
                    continue
                try:
                    from src.voice.stt import get_stt
                    stt_res = await get_stt().transcribe(
                        blob, filename=f"avengers.{audio_format}")
                except Exception as e:
                    await _safe_send(ws, {"type": "error", "message": f"STT failed: {e}"})
                    continue
                transcript = (stt_res.get("text") or "").strip()
                await _safe_send(ws, {
                    "type": "transcript", "text": transcript,
                    "language": stt_res.get("language", "en"),
                    "provider": stt_res.get("provider", "?"),
                })
                if not transcript:
                    continue

                wake_armed = bool(msg.get("wake_armed", False))
                if wake_armed:
                    detected = _wake_in(transcript)
                    await _safe_send(ws, {
                        "type": "wake", "detected": detected, "transcript": transcript})
                    if not detected:
                        continue
                    command_text = _strip_wake(transcript)
                else:
                    command_text = transcript
                if not command_text:
                    continue
                sid = msg.get("session_id") or session_id
                await _run_command(
                    ws, command_text, sid,
                    persona_override=msg.get("persona"),
                    speak=bool(msg.get("speak", True)),
                    agent_mode=bool(msg.get("agent_mode", False)),
                    stop_event=agent_stop_event,
                )

            else:
                await _safe_send(ws, {"type": "error",
                                      "message": f"unknown frame type: {mtype}"})

    except WebSocketDisconnect:
        log.info("[avengers-ws] client disconnected (%s)", session_id)
    except Exception as e:
        log.warning("[avengers-ws] socket error: %s", e)
    finally:
        if log_task is not None:
            log_task.cancel()
