"""
TRON-X Voice API  --  Phase 14: Advanced Voice Pipeline

Endpoints
---------
POST /api/voice               full round-trip: audio -> STT -> LLM -> TTS -> audio (sync)
POST /api/voice/stt           speech-to-text only
POST /api/voice/tts           text-to-speech only (full text, base64 response)
POST /api/voice/tts/stream    text-to-speech, sentence-streamed (SSE audio chunks)
POST /api/voice/stream        full round-trip streaming: audio -> STT -> LLM stream
                              -> sentence TTS -> SSE (transcript + audio chunks)
GET  /api/voice/elevenlabs/voices  list available ElevenLabs voices
GET  /api/voice/status        provider info + wake word status

Phase 14 additions
------------------
- /stream      -- sentence-streaming voice round-trip via SSE
                  TTFA drops from (full LLM time) to (first sentence ~1-2s)
- /tts/stream  -- sentence-streaming TTS for arbitrary text input
- /elevenlabs/voices -- list voices from ElevenLabs API
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.core.logger import log
from src.intelligence.orchestrator import get_orchestrator
from src.intelligence.language_profile import build_language_profile
from src.voice.stt import get_stt
from src.voice.tts import get_tts, list_elevenlabs_voices
from src.voice.state import get_voice_state_store
from src.voice.vad import get_vad

router   = APIRouter(prefix="/api/voice", tags=["voice"])
settings = get_settings()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class TTSRequest(BaseModel):
    text:    str            = Field(..., min_length=1, max_length=4000)
    persona: str            = Field(default="jarvis")
    speed:   Optional[float] = None
    format:  str            = Field(default="wav")
    lang_hint: Optional[str] = None


class TTSStreamRequest(BaseModel):
    text:    str = Field(..., min_length=1, max_length=8000)
    persona: str = Field(default="jarvis")
    lang_hint: Optional[str] = None


class VoiceModeUpdate(BaseModel):
    voice_output_enabled: Optional[bool] = None
    wake_word_enabled: Optional[bool] = None


class VoiceRoundTripResponse(BaseModel):
    transcript:     str
    reply:          str
    model:          str
    intent:         str
    persona:        str
    audio_b64:      str
    audio_format:   str
    stt_latency_ms: int
    llm_latency_ms: int
    tts_latency_ms: int
    session_id:     str


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# DeepSeek V3 via OpenRouter — primary voice LLM
_VOICE_MODEL = "openrouter/deepseek/deepseek-v4-flash"

async def _get_fast_model() -> str:
    """Pick voice LLM — DeepSeek V3 via OpenRouter (paid) preferred."""
    if settings.openrouter_api_key:
        return _VOICE_MODEL
    if settings.groq_api_key:
        return "groq/llama-3.3-70b-versatile"
    if settings.cerebras_api_key:
        return "cerebras/llama3.1-70b"
    if settings.gemini_api_key:
        return "gemini/gemini-1.5-flash"
    if settings.together_api_key:
        return "together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
    return "groq/llama-3.1-8b-instant"   # last resort


async def _llm_token_stream(
    transcript: str,
    persona: str,
    session_id: Optional[str],
) -> AsyncGenerator[str, None]:
    """
    Stream LLM tokens for a voice query using DeepSeek V3 via OpenRouter.
    Falls back to the intelligence router if OpenRouter key is absent.
    """
    try:
        import litellm
        from src.intelligence.persona import PersonaEngine

        persona_engine = PersonaEngine()
        system_prompt  = persona_engine.build_system_prompt(
            intent="fast_chat",
            persona=persona,
            extra_instructions=(
                "Respond concisely — this is a voice reply. "
                "Do not use markdown, bullet points, or code blocks. "
                "Use natural spoken language. Maximum 3 sentences unless "
                "the user explicitly asks for detail."
            ),
        )

        model   = await _get_fast_model()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": transcript},
        ]

        # Pass OpenRouter API key via litellm extra headers
        extra_kwargs = {}
        if settings.openrouter_api_key and "openrouter" in model:
            extra_kwargs["api_key"]  = settings.openrouter_api_key
            extra_kwargs["api_base"] = "https://openrouter.ai/api/v1"
            extra_kwargs["extra_headers"] = {
                "HTTP-Referer": "https://tron-x.local",
                "X-Title":     "TRON-X Voice",
            }

        response = await litellm.acompletion(
            model=model,
            messages=messages,
            stream=True,
            max_tokens=400,
            temperature=0.6,
            **extra_kwargs,
        )

        async for chunk in response:
            delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            if delta:
                yield delta

    except Exception as e:
        log.error("[voice/stream] LLM stream error (%s): %s", type(e).__name__, e)
        yield "I encountered an error. Please try again."


# ---------------------------------------------------------------------------
# Endpoints — existing (preserved)
# ---------------------------------------------------------------------------

@router.post("", response_model=VoiceRoundTripResponse)
async def voice_round_trip(
    file:       UploadFile    = File(..., description="Audio file (wav, webm, mp3, ogg)"),
    session_id: Optional[str] = Form(default=None),
    persona:    str           = Form(default="jarvis"),
    language:   Optional[str] = Form(default=None),
):
    """
    Full synchronous voice pipeline:
      audio -> VAD trim -> Groq Whisper STT -> LLM -> Kokoro/ElevenLabs TTS -> base64 audio
    """
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio file")

    stt_engine   = get_stt()
    tts_engine   = get_tts()
    vad_engine   = get_vad()
    orchestrator = get_orchestrator()

    if not vad_engine.has_speech(audio_bytes):
        raise HTTPException(422, "No speech detected in audio")
    trimmed = vad_engine.trim_silence(audio_bytes)

    stt_result = await stt_engine.transcribe(
        audio_bytes=trimmed,
        filename=file.filename or "audio.wav",
        language=language,
        prompt="JARVIS FRIDAY TRON-X",
    )
    transcript  = stt_result["text"].strip()
    stt_latency = stt_result["latency_ms"]

    if not transcript:
        raise HTTPException(422, "Could not transcribe audio — please speak clearly")

    log.info('[voice] Transcript: "%s"', transcript)

    llm_result = await orchestrator.chat(
        user_message=transcript,
        session_id=session_id,
        persona=persona,
        intent="auto",
        max_tokens=512,
    )
    reply       = llm_result["reply"]
    llm_latency = llm_result["latency_ms"]
    session_id  = llm_result["session_id"]
    language_profile = build_language_profile(
        transcript,
        stt_language=stt_result.get("language"),
    )
    get_voice_state_store().set_language_profile(language_profile, persona=persona)

    tts_result      = await tts_engine.synthesize(
        text=reply,
        persona=persona,
        lang_hint=language_profile.get("preferred_tts_lang"),
    )
    audio_bytes_out = tts_result["audio_bytes"]
    tts_latency     = tts_result["latency_ms"]

    log.info(
        "[voice] Round-trip complete — STT:%dms + LLM:%dms + TTS:%dms = %dms total",
        stt_latency, llm_latency, tts_latency,
        stt_latency + llm_latency + tts_latency,
    )

    return VoiceRoundTripResponse(
        transcript=transcript,
        reply=reply,
        model=llm_result["model"],
        intent=llm_result["intent"],
        persona=persona,
        audio_b64=base64.b64encode(audio_bytes_out).decode(),
        audio_format=tts_result["format"],
        stt_latency_ms=stt_latency,
        llm_latency_ms=llm_latency,
        tts_latency_ms=tts_latency,
        session_id=session_id,
    )


@router.post("/stt")
async def stt_only(
    file:     UploadFile    = File(...),
    language: Optional[str] = Form(default=None),
):
    """Speech-to-text only — no LLM call."""
    audio_bytes = await file.read()
    result = await get_stt().transcribe(
        audio_bytes=audio_bytes,
        filename=file.filename or "audio.wav",
        language=language,
    )
    return result


@router.post("/tts")
async def tts_only(req: TTSRequest):
    """Text-to-speech only — returns audio as base64."""
    language_profile = build_language_profile(req.text)
    result = await get_tts().synthesize(
        text=req.text,
        persona=req.persona,
        speed_override=req.speed,
        return_format=req.format,
        lang_hint=req.lang_hint or language_profile.get("preferred_tts_lang"),
    )
    if not result["audio_bytes"]:
        raise HTTPException(500, "TTS synthesis failed")

    return {
        "audio_b64":    base64.b64encode(result["audio_bytes"]).decode(),
        "audio_format": result["format"],
        "provider":     result["provider"],
        "language":     result.get("language", req.lang_hint or language_profile.get("preferred_tts_lang")),
        "latency_ms":   result["latency_ms"],
        "char_count":   result["char_count"],
    }


# ---------------------------------------------------------------------------
# Phase 14 -- NEW endpoints
# ---------------------------------------------------------------------------

@router.post("/tts/stream")
async def tts_stream(req: TTSStreamRequest):
    """
    Sentence-streaming TTS via SSE.

    Splits the input text into sentences and synthesizes each one
    independently, streaming audio chunks as they're ready.

    SSE event types:
      {"type": "audio_chunk", "audio_b64": str, "format": str,
       "sentence": str, "index": int, "latency_ms": int}
      {"type": "done", "total_chunks": int, "total_latency_ms": int}
      {"type": "error", "message": str}
    """
    tts = get_tts()
    t_start = time.monotonic()

    async def _text_gen():
        # Yield the full text as one chunk — synthesize_stream splits it
        yield req.text

    async def _generate():
        total = 0
        try:
            async for chunk in tts.synthesize_stream(
                _text_gen(),
                persona=req.persona,
                lang_hint=req.lang_hint or build_language_profile(req.text).get("preferred_tts_lang"),
            ):
                total += 1
                yield _sse({
                    "type":       "audio_chunk",
                    "audio_b64":  base64.b64encode(chunk["audio_bytes"]).decode(),
                    "format":     chunk["format"],
                    "sentence":   chunk["sentence"],
                    "index":      chunk["index"],
                    "latency_ms": chunk["latency_ms"],
                })
        except Exception as e:
            log.error("[voice/tts/stream] Error: %s", e)
            yield _sse({"type": "error", "message": str(e)})
            return

        elapsed = int((time.monotonic() - t_start) * 1000)
        yield _sse({"type": "done", "total_chunks": total, "total_latency_ms": elapsed})
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/stream")
async def voice_stream(
    file:       UploadFile    = File(..., description="Audio file (wav, webm, mp3, ogg)"),
    session_id: Optional[str] = Form(default=None),
    persona:    str           = Form(default="jarvis"),
    language:   Optional[str] = Form(default=None),
):
    """
    Streaming voice round-trip via SSE.

    Pipeline:
      audio upload -> VAD -> STT -> LLM token stream -> sentence TTS -> SSE events

    TTFA (time to first audio) is dramatically lower than the sync endpoint
    because TTS starts as soon as the first sentence of LLM output is ready,
    rather than waiting for the full response.

    SSE event sequence:
      {"type": "transcript",  "text": str}                               -- after STT
      {"type": "text_chunk",  "text": str}                               -- each LLM token
      {"type": "audio_chunk", "audio_b64": str, "format": str,
       "sentence": str, "index": int, "tts_latency_ms": int}             -- per sentence
      {"type": "done", "stats": {
          "stt_latency_ms":   int,
          "llm_latency_ms":   int,
          "audio_chunks":     int,
          "total_latency_ms": int,
       }}
      {"type": "error", "message": str}                                  -- on failure
    """
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(400, "Empty audio file")

    t_start = time.monotonic()

    # -- 1. VAD + STT (blocking — must complete before streaming starts) ------
    vad = get_vad()
    if not vad.has_speech(audio_bytes):
        raise HTTPException(422, "No speech detected in audio")
    trimmed = vad.trim_silence(audio_bytes)

    stt_result = await get_stt().transcribe(
        audio_bytes=trimmed,
        filename=file.filename or "audio.wav",
        language=language,
        prompt="JARVIS FRIDAY TRON-X",
    )
    transcript  = stt_result["text"].strip()
    stt_latency = stt_result["latency_ms"]

    if not transcript:
        raise HTTPException(422, "Could not transcribe audio — please speak clearly")

    log.info('[voice/stream] Transcript: "%s"', transcript)

    tts = get_tts()

    async def _generate():
        audio_chunks = 0
        full_reply   = []
        llm_t0       = time.monotonic()

        # Transcript event
        yield _sse({"type": "transcript", "text": transcript})

        # -- 2. LLM stream with concurrent sentence TTS ----------------------
        # We use a queue to decouple the LLM token producer from the TTS consumer.
        # This lets us stream text_chunk events while TTS is running.

        token_queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=256)
        sse_queue: asyncio.Queue[Optional[str]] = asyncio.Queue(maxsize=64)

        # Producer: LLM tokens -> token_queue
        async def _produce_tokens():
            try:
                async for token in _llm_token_stream(transcript, persona, session_id):
                    full_reply.append(token)
                    await token_queue.put(token)
            finally:
                await token_queue.put(None)   # sentinel

        # Text relay: token_queue -> sse_queue (text_chunk events)
        async def _relay_text():
            async def _token_gen():
                while True:
                    tok = await token_queue.get()
                    if tok is None:
                        return
                    yield tok

            async for chunk in tts.synthesize_stream(_token_gen(), persona=persona):
                # Queue the text event first
                await sse_queue.put(_sse({
                    "type":            "audio_chunk",
                    "audio_b64":       base64.b64encode(chunk["audio_bytes"]).decode(),
                    "format":          chunk["format"],
                    "sentence":        chunk["sentence"],
                    "index":           chunk["index"],
                    "tts_latency_ms":  chunk["latency_ms"],
                }))
            await sse_queue.put(None)   # sentinel

        # Start both concurrently
        producer_task = asyncio.create_task(_produce_tokens())
        relay_task    = asyncio.create_task(_relay_text())

        # Drain SSE queue while tasks run
        done_sentinel = False
        while not done_sentinel:
            try:
                event = await asyncio.wait_for(sse_queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield _sse({"type": "error", "message": "Voice stream timed out"})
                producer_task.cancel()
                relay_task.cancel()
                return

            if event is None:
                done_sentinel = True
            else:
                audio_chunks += 1
                yield event

        await producer_task
        await relay_task

        llm_latency   = int((time.monotonic() - llm_t0) * 1000)
        total_latency = int((time.monotonic() - t_start) * 1000)

        log.info(
            "[voice/stream] Done — STT:%dms LLM+TTS:%dms chunks:%d total:%dms",
            stt_latency, llm_latency, audio_chunks, total_latency,
        )

        yield _sse({
            "type": "done",
            "reply": "".join(full_reply),
            "stats": {
                "stt_latency_ms":   stt_latency,
                "llm_latency_ms":   llm_latency,
                "audio_chunks":     audio_chunks,
                "total_latency_ms": total_latency,
            },
        })

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/elevenlabs/voices")
async def elevenlabs_voices():
    """List available ElevenLabs voices. Returns empty list if no API key configured."""
    voices = await list_elevenlabs_voices()
    return {
        "voices":    voices,
        "count":     len(voices),
        "available": bool(voices),
        "configured_jarvis": settings.elevenlabs_voice_jarvis,
        "configured_friday": settings.elevenlabs_voice_friday,
    }


@router.get("/status")
async def voice_status():
    tts = get_tts()
    vad = get_vad()
    state = get_voice_state_store().get()
    try:
        from src.voice.wake_word import get_wake_word_detector
        wwd  = get_wake_word_detector()
        wake = {"available": wwd.available, "running": wwd.running}
    except Exception:
        wake = {"available": False, "running": False}

    return {
        "tts": {
            "provider":            tts.provider,
            "elevenlabs":          tts.elevenlabs_available,
            "elevenlabs_model":    settings.elevenlabs_model if tts.elevenlabs_available else None,
            "kokoro":              tts._kokoro_available,
            "edge_tts":            True,
        },
        "stt": {
            "provider": "groq_whisper" if get_stt()._groq_client else "local_whisper",
        },
        "vad": {
            "available": vad.available,
        },
        "wake_word": wake,
        "voice_mode": state,
        "streaming": {
            "supported":          True,
            "sentence_streaming": True,
            "endpoints": [
                "POST /api/voice/stream",
                "POST /api/voice/tts/stream",
            ],
        },
    }


@router.get("/mode")
async def get_voice_mode():
    return get_voice_state_store().get()


@router.post("/mode")
async def update_voice_mode(body: VoiceModeUpdate):
    store = get_voice_state_store()
    updates = {}
    if body.voice_output_enabled is not None:
        updates["voice_output_enabled"] = body.voice_output_enabled
    if body.wake_word_enabled is not None:
        updates["wake_word_enabled"] = body.wake_word_enabled
        try:
            from src.voice.wake_word import get_wake_word_detector
            detector = get_wake_word_detector()
            if body.wake_word_enabled:
                detector.start()
            else:
                detector.stop()
        except Exception as exc:
            log.warning("[voice_mode] Wake-word toggle failed: %s", exc)
    if updates:
        store.update(**updates)
    return store.get()
