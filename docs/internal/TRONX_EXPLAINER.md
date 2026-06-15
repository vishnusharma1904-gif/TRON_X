# TRON-X — Complete Project Explainer
> Everything you need to explain this project to anyone — structure, tech stack, functions, and challenges.

---

## What is TRON-X?

TRON-X is a **personal AI assistant for Windows** — think Jarvis/Friday from Iron Man. It runs as a local server on your machine, exposes a REST API, and has a Three.js HUD frontend. You can talk to it via voice, give it commands in natural language, and it will autonomously delegate tasks to specialized sub-agents (browse the web, read your emails, control your smart home, run code, etc.).

**One-line pitch:** "A self-hosted, multi-model AI assistant that can see your screen, control your PC, talk to you, manage your calendar, and automate tasks — all from a local FastAPI server."

---

## Tech Stack

| Layer | Technology | Why |
|---|---|---|
| **Web framework** | FastAPI + Uvicorn | Async Python, auto-generates OpenAPI docs, fast |
| **LLM routing** | LiteLLM | Unified API for 104 models across 14 providers (OpenAI, Anthropic, Groq, Gemini, etc.) |
| **Vector memory** | ChromaDB | Local vector database for semantic search / RAG |
| **Embeddings** | SentenceTransformers (`all-MiniLM-L6-v2`) | 384-dim, runs on CPU, no GPU needed |
| **Cloud DB** | Supabase | Optional persistent chat history |
| **Voice STT** | Groq Whisper API | Fast speech-to-text |
| **Voice TTS** | Kokoro-ONNX (local) → ElevenLabs → edge-tts → pyttsx3 | Cascading quality/fallback chain |
| **Browser control** | Playwright (Chromium) | Headless browser for scraping and automation |
| **Scheduling** | APScheduler | Cron jobs and one-shot reminders |
| **IoT** | Home Assistant REST API + MQTT (paho) | Smart home device control |
| **Email** | imaplib/smtplib (stdlib) | Read + send email, no extra deps |
| **Calendar** | Google Calendar API (OAuth2) | Event management |
| **System control** | psutil + pyautogui + PowerShell | OS-level automation on Windows |
| **Screen/OCR** | mss + Tesseract + EasyOCR | Screenshots and text extraction |
| **Config** | Pydantic Settings + python-dotenv | Type-safe env var loading |
| **Data validation** | Pydantic v2 | Request/response models for every API endpoint |
| **Logging** | Rich (console) + Python logging | Coloured terminal output |
| **Containerisation** | Docker + docker-compose | Optional deployment |
| **Service** | systemd (tronx.service) | Linux production deployment |

---

## Project Structure (Folder by Folder)

```
D:\Tron_X\
│
├── src/                        ← All application code
│   ├── main.py                 ← FastAPI app: routers registered, middleware, lifespan
│   │
│   ├── core/                   ← Cross-cutting concerns
│   │   ├── config.py           ← Pydantic Settings — reads .env
│   │   ├── logger.py           ← Singleton logger with Rich console
│   │   ├── exceptions.py       ← Custom exception hierarchy
│   │   ├── auth.py             ← API key auth middleware (Phase 20)
│   │   └── ratelimit.py        ← Per-IP rate limiting middleware (Phase 20)
│   │
│   ├── intelligence/           ← "The brain" — LLM logic
│   │   ├── router.py           ← SmartRouter: 104 models, circuit-breaker, A/B tests, latency tracking
│   │   ├── orchestrator.py     ← Main chat pipeline: intent → RAG → persona → LLM → post-process
│   │   ├── intent.py           ← IntentClassifier: keyword fast-path + LLM verification
│   │   ├── persona.py          ← PersonaEngine: Jarvis/Friday system prompts
│   │   ├── cot.py              ← Chain-of-Thought injection for math/reasoning
│   │   ├── prompts.py          ← Prompt templates
│   │   ├── commands.py         ← Action commands (e.g. "send WhatsApp to X")
│   │   └── router.py           ← See above
│   │
│   ├── api/                    ← REST endpoints (one file per feature domain)
│   │   ├── chat.py             ← POST /api/chat (+ streaming)
│   │   ├── memory.py           ← POST /api/memory/ingest, search, stats
│   │   ├── voice.py            ← POST /api/voice/stt, tts, stream-tts
│   │   ├── system.py           ← /api/system: files, browser, screen, execute, powershell
│   │   ├── iot.py              ← /api/iot: lights, switches, sensors, MQTT
│   │   ├── agents.py           ← /api/agents: research, coordinator, scheduler, pipeline
│   │   ├── calendar.py         ← /api/calendar + /api/calendar/reminders
│   │   ├── email.py            ← /api/email: fetch, read, search, summarize, draft
│   │   ├── whatsapp.py         ← /api/whatsapp
│   │   ├── episodic.py         ← /api/memory/episodic
│   │   ├── feeds.py            ← /api/feeds: weather, stocks, news, crypto
│   │   ├── analytics.py        ← /api/analytics: usage stats, latency charts
│   │   ├── plugins.py          ← /api/plugins: dynamic plugin management
│   │   └── health.py           ← GET /api/health, /api/latency/stats
│   │
│   ├── agents/                 ← Autonomous sub-agents
│   │   ├── coordinator.py      ← TaskCoordinator: registry + parallel/sequential dispatch
│   │   ├── research_agent.py   ← ResearchAgentV2: web search (Brave/Serper/DDG) + synthesis
│   │   ├── browser_agent.py    ← BrowserAgent: Playwright singleton, navigate/scrape/click
│   │   ├── calendar_agent.py   ← Google Calendar OAuth2 CRUD
│   │   ├── reminder_agent.py   ← Windows toast reminders (winotify)
│   │   ├── email_agent.py      ← IMAP email reader + LLM summarizer
│   │   ├── scheduler_agent.py  ← APScheduler wrapper + NL schedule parsing
│   │   ├── task_decomposer.py  ← LLM-planned multi-agent pipelines
│   │   ├── whatsapp_agent.py   ← WhatsApp automation
│   │   ├── whatsapp_bridge.py  ← WhatsApp connection layer
│   │   ├── whatsapp_contacts.py← Contact lookup
│   │   ├── code_agent.py       ← Code generation/execution agent
│   │   ├── vision_agent.py     ← Screen vision agent
│   │   └── cad_agent.py        ← 3D CAD model generation (CadQuery)
│   │
│   ├── memory/                 ← Memory and retrieval
│   │   ├── chroma_db.py        ← ChromaDB manager: 4 collections
│   │   ├── embeddings.py       ← SentenceTransformers wrapper (@lru_cache)
│   │   ├── rag.py              ← RAG pipeline: search → MMR rerank → inject
│   │   ├── episodic_memory.py  ← Episodic memory: remember/recall/summarize sessions
│   │   ├── ingestion.py        ← Ingest PDFs, Word docs, text, web pages
│   │   └── supabase_client.py  ← Optional cloud persistence
│   │
│   ├── system/                 ← OS-level control
│   │   ├── control.py          ← Volume, brightness, processes, services
│   │   ├── powershell.py       ← Safe PowerShell runner with whitelist + blocked patterns
│   │   ├── files.py            ← File ops: search, read, copy, move, organize, archive
│   │   ├── executor.py         ← Sandboxed code execution: Python/JS/Bash
│   │   ├── browser.py          ← Simple browser (non-Playwright)
│   │   ├── email_client.py     ← SMTP email sender
│   │   └── whatsapp_client.py  ← WhatsApp client
│   │
│   ├── voice/                  ← Voice I/O
│   │   ├── tts.py              ← TTS chain: ElevenLabs → Kokoro → edge-tts → pyttsx3
│   │   ├── stt.py              ← STT via Groq Whisper
│   │   ├── vad.py              ← Voice activity detection
│   │   └── wake_word.py        ← Wake word detection
│   │
│   ├── vision/
│   │   └── screen.py           ← Screenshot, OCR (Tesseract/EasyOCR), vision LLM description
│   │
│   ├── iot/                    ← Smart home
│   │   ├── home_assistant.py   ← HA REST client: states, scenes, scripts, automations
│   │   ├── mqtt_client.py      ← MQTT publish/subscribe (paho)
│   │   ├── device_groups.py    ← Group devices (e.g. "all bedroom lights")
│   │   ├── nl_mapper.py        ← NL → device command mapping
│   │   └── ws_listener.py      ← Home Assistant WebSocket real-time event listener
│   │
│   ├── feeds/                  ← Real-time data
│   │   ├── weather.py          ← Weather API
│   │   ├── stocks.py           ← Stock prices
│   │   ├── news.py             ← News headlines
│   │   └── crypto.py           ← Crypto prices
│   │
│   ├── analytics/              ← Usage tracking
│   │   ├── collector.py        ← Records API calls, agent invocations, latency
│   │   └── middleware.py       ← FastAPI middleware for fire-and-forget analytics
│   │
│   └── plugins/                ← Plugin system
│       ├── plugin_registry.py  ← Scans plugin dir, loads JSON manifests
│       └── plugin_manifest.py  ← Manifest schema (name, version, agents, routes)
│
├── config/
│   ├── models.json             ← 104 models across 14 providers + safe params per provider
│   ├── personas.json           ← Jarvis/Friday persona definitions
│   └── settings.yaml           ← App settings
│
├── memory/
│   ├── chroma/                 ← ChromaDB vector store files
│   ├── cache/sessions.json     ← Chat session persistence
│   ├── whatsapp_contacts.json  ← Cached WhatsApp contacts
│   └── whatsapp_messages.json  ← Cached WhatsApp messages
│
├── models/
│   ├── kokoro-v1.1-zh.onnx     ← Local TTS model (Kokoro, 82M params)
│   └── voices-v1.1-zh.bin      ← Voice embeddings for Kokoro
│
├── logs/
│   └── tron_x.log              ← Application log
│
├── static/                     ← Three.js HUD frontend
│
├── kokoro-onnx/                ← Kokoro TTS library (submodule)
├── deploy/tronx.service        ← systemd service file
├── Dockerfile                  ← Docker image definition
├── docker-compose.yml          ← Docker Compose config
├── requirements.txt            ← Python dependencies
└── .env / .env.example         ← API keys and config
```

---

## How a Chat Request Flows (End-to-End)

This is the single most important thing to understand — the full pipeline every message goes through:

```
User sends: "What are the latest AI breakthroughs?"
             ↓
1. POST /api/chat  →  chat.py API route
             ↓
2. orchestrator.chat()
             ↓
3. IntentClassifier.classify()
   → keyword scan: finds "latest" → "research"
   → confidence > 0.8? done. else ask LLM to verify.
             ↓
4. RAG.should_use_rag()  →  YES (research intent)
   RAG.retrieve("latest AI breakthroughs")
   → embed query → search ChromaDB (all 4 collections)
   → MMR rerank → top 5 chunks → inject into system prompt
             ↓
5. PersonaEngine.build_system_prompt("jarvis", "research", rag_context)
   → "You are JARVIS... [RAG context] ... answer the question"
             ↓
6. CoTHandler.needs_cot("research") → NO (CoT for math/medical/reasoning only)
             ↓
7. SmartRouter.complete(messages, category="long_context",
                        preferred_model="openrouter/google/gemma-3-27b-it:free")
   → Check health tracker (is model circuit-broken?)
   → Check rate limiter (RPM window)
   → Call LiteLLM.acompletion() with retry (2 attempts, exponential backoff)
   → On failure: try next model in chain
             ↓
8. Post-process:
   → cot_handler.extract_thinking() → strip <think> blocks
   → persona_engine.sanitize_response() → strip filler phrases
             ↓
9. Persist:
   → Save turn to session (memory/cache/sessions.json)
   → rag.store_turn() → embed + save to ChromaDB conversations collection
   → supabase.save_message() (optional cloud backup)
             ↓
10. Return: {reply, model, session_id, intent, confidence, tokens_used, latency_ms}
```

---

## Key Functions Explained

### `SmartRouter._get_chain(category, preferred_model)`
Builds an ordered list of models to try for a given task category. It:
1. Filters out providers without API keys
2. For speed-sensitive categories (fast_chat, coding), re-sorts by measured P50 latency
3. Injects the A/B test winner at the front if an experiment is running
4. If a `preferred_model` is specified (e.g., DeepSeek-R1 for math), it goes first unconditionally

### `HealthTracker`
A circuit-breaker per model. After 3 failures it "trips" — the model is skipped for 120 seconds. After cooldown, it gets one probe attempt. This prevents cascading failures when a provider is down.

### `RAGPipeline.retrieve(query)`
1. Embeds the query with SentenceTransformers
2. Searches all 4 ChromaDB collections simultaneously
3. Filters hits below 0.40 similarity score
4. Applies **MMR (Maximal Marginal Relevance)** reranking — picks chunks that are both relevant AND diverse (λ=0.5), avoiding redundant context

### `EpisodicMemoryAgent.remember(user_msg, assistant_reply)`
After each chat turn, the LLM extracts structured metadata: `{summary, topic, entities, emotion}`. This is stored in ChromaDB's `episodes` collection. Later, `recall(query)` can find relevant past conversations semantically — like the AI remembering "last Tuesday we discussed your project deadline."

### `TaskCoordinator.run_parallel(tasks)` / `stream_parallel(tasks)`
The multi-agent dispatcher. Given a list like `[{agent: "research_v2", payload: {query: "..."}}, {agent: "screenshot", payload: {}}]`, it fires all tasks with `asyncio.gather` simultaneously. The streaming version uses `asyncio.wait(FIRST_COMPLETED)` to emit SSE events as each agent finishes — the frontend can show real-time progress.

### `execute_python_safe(code)`
A sandboxed Python executor that:
1. Walks the AST to block forbidden imports (`os`, `subprocess`, `socket`, `ctypes`, etc.)
2. Blocks `exec()`, `eval()`, `__import__()`
3. Runs in a subprocess with a 15-second timeout
4. Auto-installs missing packages with pip if `auto_install=True`

### `synthesize_stream(text_stream)`
Sentence-streaming TTS. Instead of waiting for the full LLM response then synthesizing, it listens to the LLM token stream, splits on sentence boundaries (`[.!?]`), and synthesizes each sentence as it arrives. Time-to-first-audio drops from "full LLM response time" to ~1–2 seconds.

---

## The 20 Development Phases

TRON-X was built incrementally in 20 phases:

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

One of TRON-X's core design choices is **never being locked to one LLM provider**. The `config/models.json` defines 104 models across 14 providers, each in a category:

| Category | Primary Model | Use Case |
|---|---|---|
| `fast_chat` | Groq Llama-3.3-70B | Everyday chat (fast, cheap) |
| `reasoning` | DeepSeek-R1 (Together AI) | Math, logic, medical |
| `coding` | Qwen2.5-Coder-32B | Code generation |
| `vision` | Gemini 2.0 Flash | Screen/image analysis |
| `long_context` | Gemma-3-27B (OpenRouter) | Research, long documents |
| `academic` | DeepSeek-R1 | Technical/academic writing |

If the primary model fails (rate limit, auth error, timeout), it automatically cascades through the fallback chain. If you have no API keys at all, it logs a warning but still starts.

---

## Memory Architecture (4 ChromaDB Collections)

```
ChromaDB
├── conversations    ← Every chat turn, embedded for future RAG retrieval
├── documents        ← Ingested files (PDFs, Word docs, web pages, notes)
├── knowledge        ← Manually added facts ("My dog's name is Rex")
└── episodes         ← Episodic memory: summarized sessions with topic/emotion metadata
```

All embeddings use `all-MiniLM-L6-v2` (384 dimensions, CPU-only, ~80MB). The model is cached with `@lru_cache` and pre-warmed on startup to avoid the 4–8 second cold-start delay on the first request.

---

## Key Design Patterns Used Throughout

**1. Singleton with lazy init**
```python
_orchestrator: Orchestrator | None = None
def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
```
Used for: orchestrator, router, ChromaDB, RAG, all agents. Ensures one instance per process, initialized only when first needed.

**2. Blocking I/O in async context**
```python
result = await asyncio.get_event_loop().run_in_executor(None, blocking_fn)
```
All synchronous I/O (file ops, IMAP, Google Calendar API, psutil) is wrapped this way to avoid blocking the FastAPI event loop.

**3. Confirm guard on destructive operations**
```python
if not confirm:
    return {"error": "Set confirm=True to proceed", "preview": what_would_happen}
```
Every delete/overwrite/kill operation requires explicit `confirm: bool = True` in the request body.

**4. SSE streaming pattern**
```python
def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

return StreamingResponse(generator(), media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```
Used for: chat streaming, research streaming, multi-agent parallel streaming.

**5. Agent registry via decorator**
```python
@register_agent("research_v2", "Web research with provider cascade")
async def _agent_research_v2(payload: dict) -> dict:
    ...
```
Any function decorated with `@register_agent` becomes callable through the TaskCoordinator by name.

**6. Auto-install on ImportError**
```python
try:
    import some_package
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "some_package",
                    "--break-system-packages", "--quiet"], check=True)
    import some_package
```
Heavy optional deps (Google Calendar client, winotify, OCR engines) are installed on first use rather than at startup.

---

## Challenges Faced

### 1. Async + Blocking Libraries
Most Python libraries (imaplib, Google Calendar, psutil, Playwright sync API) are synchronous. Every call had to be wrapped in `run_in_executor()`. Missing this causes the entire FastAPI server to freeze while one request waits for I/O.

### 2. LiteLLM Provider Inconsistencies
Different providers support different parameters. For example, some don't support `temperature`, others don't support `max_tokens`. The `_filter_params()` function and `safe_params` per-provider config in `models.json` solve this — unsupported params are silently dropped before the API call.

### 3. Playwright Singleton and Event Loop Timing
The browser agent uses an `asyncio.Lock` to prevent two requests from launching two browser instances simultaneously. The lock can't be a class variable because the event loop doesn't exist when the class is defined — it has to be lazily initialized with a factory function after the loop is running.

### 4. Token Budget Management
LLMs have context limits. If you keep all chat history, you'll eventually exceed the limit and get errors. The `_trim_history()` function pops the oldest user/assistant message pairs until the token count (measured with tiktoken) is under 8,000 tokens.

### 5. TTS Latency (Time-to-First-Audio)
The naive approach — wait for full LLM response, then synthesize — adds 3–8 seconds of silence. Phase 14 solved this with sentence-streaming TTS: the LLM token stream is split on sentence boundaries, and each sentence is synthesized immediately, reducing TTFA to ~1–2 seconds.

### 6. Unicode Characters Breaking File Writes
Python's file writing tools silently truncate files when they encounter certain Unicode box-drawing characters (like `──`). All code uses plain ASCII characters, and file writes go through bash `cat > file << 'ENDOFFILE'` to avoid this.

### 7. RAG Cold Start
The SentenceTransformers model takes 4–8 seconds to load the first time. Without pre-warming, the first user message would stall. The `lifespan` startup handler calls `embed(["warmup"])` to load the model before any requests arrive.

### 8. CadQuery Python 3.13 Incompatibility
The 3D CAD generation agent uses CadQuery, which is incompatible with Python 3.13. The dependency is commented out in `requirements.txt` with instructions to install via conda on Python 3.11 if needed.

### 9. Plugin System Security
Dynamically loading code from JSON manifests (Phase 18) means untrusted plugins could run arbitrary code. The plugin registry validates manifests against a strict schema and sandboxes agent functions through the existing executor infrastructure.

### 10. WhatsApp Automation Fragility
WhatsApp Web automation via Playwright is inherently fragile — UI changes break selectors, and the session requires QR code re-authentication. The bridge layer abstracts this but remains the least reliable component.

---

## Running the Project

```bash
# 1. Clone and setup
cd D:\Tron_X
pip install -r requirements.txt
playwright install chromium

# 2. Configure
cp .env.example .env
# Edit .env — add your API keys (at minimum: GROQ_API_KEY for voice/fast chat)

# 3. Run
uvicorn src.main:app --reload --port 8000

# 4. Open HUD
# http://127.0.0.1:8000  →  redirects to /static/index.html
# API docs: http://127.0.0.1:8000/docs
```

---

## API Surface (Quick Reference)

| Prefix | What it controls |
|---|---|
| `/api/chat` | Chat with LLM (streaming + non-streaming) |
| `/api/memory` | Ingest documents, search memory, manage RAG |
| `/api/memory/episodic` | Episode storage: remember/recall/summarize sessions |
| `/api/voice` | STT (transcribe), TTS (speak), streaming TTS |
| `/api/system` | Files, browser, screen, execute code, powershell, processes |
| `/api/iot` | Home Assistant devices, MQTT, scenes, automations |
| `/api/agents` | Research, multi-agent pipelines, scheduler |
| `/api/calendar` | Google Calendar events + reminders |
| `/api/email` | IMAP fetch/search/summarize + SMTP send |
| `/api/whatsapp` | WhatsApp send/receive |
| `/api/feeds` | Weather, stocks, news, crypto |
| `/api/analytics` | Usage stats, latency charts |
| `/api/plugins` | Load/list dynamic plugins |
| `/api/health` | Health check, provider status, latency stats |
