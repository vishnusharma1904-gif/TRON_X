# TRON-X — Architecture & Design Notes

## How a Chat Request Flows (End-to-End)

```
User sends: "What are the latest AI breakthroughs?"
             |
1. POST /api/chat  ->  chat.py API route
             |
2. orchestrator.chat()
             |
3. IntentClassifier.classify()
   - keyword scan: finds "latest" -> "research"
   - confidence > 0.8? done. else ask LLM to verify.
             |
4. RAG.should_use_rag()  ->  YES (research intent)
   RAG.retrieve("latest AI breakthroughs")
   -> embed query -> search ChromaDB (all 4 collections)
   -> MMR rerank -> top 5 chunks -> inject into system prompt
             |
5. PersonaEngine.build_system_prompt("jarvis", "research", rag_context)
   -> "You are JARVIS... [RAG context] ... answer the question"
             |
6. CoTHandler.needs_cot("research") -> NO (CoT for math/medical/reasoning only)
             |
7. SmartRouter.complete(messages, category="long_context",
                        preferred_model="openrouter/google/gemma-3-27b-it:free")
   -> Check health tracker (is model circuit-broken?)
   -> Check rate limiter (RPM window)
   -> Call LiteLLM.acompletion() with retry (2 attempts, exponential backoff)
   -> On failure: try next model in chain
             |
8. Post-process:
   -> cot_handler.extract_thinking() -> strip <think> blocks
   -> persona_engine.sanitize_response() -> strip filler phrases
             |
9. Persist:
   -> Save turn to session (memory/cache/sessions.json)
   -> rag.store_turn() -> embed + save to ChromaDB conversations collection
   -> supabase.save_message() (optional cloud backup)
             |
10. Return: {reply, model, session_id, intent, confidence, tokens_used, latency_ms}
```

---

## Key Functions

### `SmartRouter._get_chain(category, preferred_model)`
Builds an ordered list of models to try for a given task category:
1. Filters out providers without API keys
2. For speed-sensitive categories (fast_chat, coding), re-sorts by measured P50 latency
3. Injects the A/B test winner at the front if an experiment is running
4. If a `preferred_model` is specified (e.g. DeepSeek-R1 for math), it goes first unconditionally

### `HealthTracker`
A circuit-breaker per model. After 3 failures it "trips" — the model is skipped for 120 seconds. After cooldown, it gets one probe attempt. Prevents cascading failures when a provider is down.

### `RAGPipeline.retrieve(query)`
1. Embeds the query with SentenceTransformers
2. Searches all 4 ChromaDB collections simultaneously
3. Filters hits below 0.40 similarity score
4. Applies MMR (Maximal Marginal Relevance) reranking — picks chunks that are both relevant AND diverse (λ=0.5)

### `EpisodicMemoryAgent.remember(user_msg, assistant_reply)`
After each chat turn, the LLM extracts structured metadata: `{summary, topic, entities, emotion}`, stored in ChromaDB's `episodes` collection. `recall(query)` later finds relevant past conversations semantically.

### `TaskCoordinator.run_parallel(tasks)` / `stream_parallel(tasks)`
Multi-agent dispatcher. Given a list like `[{agent: "research_v2", payload: {...}}, {agent: "screenshot", payload: {}}]`, fires all tasks with `asyncio.gather`. The streaming version uses `asyncio.wait(FIRST_COMPLETED)` to emit SSE events as each agent finishes.

### `execute_python_safe(code)`
Sandboxed Python executor:
1. Walks the AST to block forbidden imports (`os`, `subprocess`, `socket`, `ctypes`, etc.)
2. Blocks `exec()`, `eval()`, `__import__()`
3. Runs in a subprocess with a 15-second timeout
4. Auto-installs missing packages with pip if `auto_install=True`

### `synthesize_stream(text_stream)`
Sentence-streaming TTS — splits the LLM token stream on sentence boundaries (`[.!?]`) and synthesizes each sentence as it arrives. Reduces time-to-first-audio from "full LLM response time" to ~1–2 seconds.

---

## Development Phases

TRON-X was built incrementally across 20 phases:

| Phase | What Was Built |
|---|---|
| 1 | FastAPI skeleton, health endpoint, basic chat |
| 2 | ChromaDB memory, RAG pipeline, document ingestion |
| 3 | Smart router (LiteLLM), intent classification, personas, CoT, A/B testing |
| 4 | System control: PowerShell runner, process/service management |
| 5 | File system agent: duplicates, batch rename, folder organizer, archives |
| 6 | Browser agent (Playwright singleton): navigate, scrape, fill forms |
| 7 | Code execution sandbox: Python/JS/Bash with AST safety checks |
| 8 | Screen capture + OCR + vision LLM description |
| 9 | Web research agent v2: Brave/Serper/DuckDuckGo cascade + synthesis |
| 10 | Multi-agent TaskCoordinator: parallel, sequential, SSE streaming |
| 11 | Google Calendar OAuth2 + Windows toast reminders |
| 12 | Email agent: IMAP reader, LLM inbox summarizer, reply drafts |
| 13 | Episodic memory: per-session episode storage with LLM extraction |
| 14 | Advanced TTS: ElevenLabs + sentence-stream TTS (TTFA optimization) |
| 15 | Real-time data feeds: weather, stocks, news, crypto |
| 16 | IoT expansion: Home Assistant scenes, scripts, automations + MQTT |
| 17 | Analytics dashboard: API call counts, agent usage, latency charts |
| 18 | Plugin system: dynamic agent loading from JSON manifests |
| 19 | Three.js HUD frontend |
| 20 | Production hardening: auth middleware, rate limiting, Docker, systemd |

---

## Multi-Provider LLM Strategy

`config/models.json` defines 100+ models across 14 providers, grouped by task category:

| Category | Primary Model | Use Case |
|---|---|---|
| `fast_chat` | Groq Llama-3.3-70B | Everyday chat (fast, cheap) |
| `reasoning` | DeepSeek-R1 (Together AI) | Math, logic, medical |
| `coding` | Qwen2.5-Coder-32B | Code generation |
| `vision` | Gemini 2.0 Flash | Screen/image analysis |
| `long_context` | Gemma-3-27B (OpenRouter) | Research, long documents |
| `academic` | DeepSeek-R1 | Technical/academic writing |

If the primary model fails (rate limit, auth error, timeout), the router automatically cascades through the fallback chain.

---

## Memory Architecture (4 ChromaDB Collections)

```
ChromaDB
├── conversations    <- Every chat turn, embedded for future RAG retrieval
├── documents        <- Ingested files (PDFs, Word docs, web pages, notes)
├── knowledge        <- Manually added facts ("My dog's name is Rex")
└── episodes         <- Episodic memory: summarized sessions with topic/emotion metadata
```

All embeddings use `all-MiniLM-L6-v2` (384 dimensions, CPU-only, ~80MB), cached with `@lru_cache` and pre-warmed on startup.

---

## Key Design Patterns

**1. Singleton with lazy init**
```python
_orchestrator: Orchestrator | None = None
def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
```

**2. Blocking I/O in async context**
```python
result = await asyncio.get_event_loop().run_in_executor(None, blocking_fn)
```

**3. Confirm guard on destructive operations**
```python
if not confirm:
    return {"error": "Set confirm=True to proceed", "preview": what_would_happen}
```

**4. SSE streaming pattern**
```python
def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

return StreamingResponse(generator(), media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

**5. Agent registry via decorator**
```python
@register_agent("research_v2", "Web research with provider cascade")
async def _agent_research_v2(payload: dict) -> dict:
    ...
```

**6. Auto-install on ImportError**
```python
try:
    import some_package
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "some_package",
                    "--break-system-packages", "--quiet"], check=True)
    import some_package
```

---

## Engineering Challenges & Solutions

1. **Async + blocking libraries** — imaplib, Google Calendar, psutil, Playwright sync API are all synchronous. Every call is wrapped in `run_in_executor()` to avoid freezing the FastAPI event loop.

2. **LiteLLM provider inconsistencies** — different providers support different parameters (`temperature`, `max_tokens`, etc.). `_filter_params()` plus per-provider `safe_params` in `models.json` silently drop unsupported params.

3. **Playwright singleton & event loop timing** — the browser agent uses an `asyncio.Lock`, lazily initialized via a factory function once the event loop is running, to prevent two requests launching duplicate browser instances.

4. **Token budget management** — `_trim_history()` pops the oldest user/assistant message pairs (measured with tiktoken) until history is under 8,000 tokens.

5. **TTS latency (time-to-first-audio)** — sentence-streaming TTS splits the LLM token stream on sentence boundaries and synthesizes each sentence immediately, reducing TTFA from 3–8s to ~1–2s.

6. **RAG cold start** — the SentenceTransformers model takes 4–8s to load on first use. The `lifespan` startup handler pre-warms it with `embed(["warmup"])`.

7. **CadQuery / Python 3.13 incompatibility** — the 3D CAD agent depends on CadQuery, which doesn't support Python 3.13. It's commented out in `requirements.txt` with conda install instructions for Python 3.11.

8. **Plugin system security** — dynamically loaded plugins (Phase 18) are validated against a strict manifest schema and run through the existing sandboxed executor.

9. **WhatsApp automation fragility** — WhatsApp Web automation via Playwright is the least reliable component; UI changes break selectors and sessions require QR re-authentication.
