# TRON-X — Complete Project Handoff
> Paste this entire file into a new conversation to continue development.

---

## Project Overview
**TRON-X** is a personal AI assistant (Jarvis/Friday) built with:
- **Backend**: FastAPI + Uvicorn (`uvicorn src.main:app --reload --port 8000`)
- **Frontend**: Three.js HUD + vanilla JS chat panel
- **LLM routing**: LiteLLM smart router with 104 open-source models
- **Voice**: edge-tts (primary) → pyttsx3 (offline fallback), Web Audio API playback
- **Memory**: ChromaDB vector store + session JSON
- **Location**: `D:\Tron_X`

---

## Complete File Structure
```
D:\Tron_X\
├── src/
│   ├── main.py                    # FastAPI app, middleware, lifespan
│   ├── core/
│   │   ├── config.py              # All settings + 13 provider API keys
│   │   ├── logger.py              # Rich logger
│   │   └── exceptions.py          # Custom exceptions
│   ├── intelligence/
│   │   ├── orchestrator.py        # Main chat() + chat_stream() + session mgmt
│   │   ├── router.py              # SmartRouter — 104 models, failover, circuit breaker
│   │   ├── intent.py              # 2-stage classifier (keywords + LLM)
│   │   ├── persona.py             # JARVIS / FRIDAY persona engine
│   │   ├── prompts.py             # System prompts per persona/intent
│   │   └── cot.py                 # Chain-of-thought handler
│   ├── api/
│   │   ├── chat.py                # POST /api/chat, POST /api/chat/stream (SSE), GET sessions/history
│   │   ├── voice.py               # POST /api/voice (STT+TTS), POST /api/voice/tts
│   │   ├── health.py              # GET /health, GET /providers, GET /models
│   │   ├── memory.py              # GET /api/memory/stats
│   │   ├── system.py              # POST /api/volume, /app/open, /screenshot, /files/*
│   │   ├── iot.py                 # IoT / Home Assistant endpoints
│   │   └── agents.py              # Agent execution endpoints
│   ├── voice/
│   │   ├── tts.py                 # TTSEngine: Kokoro → edge-tts → pyttsx3
│   │   ├── stt.py                 # Speech-to-text (whisper/faster-whisper)
│   │   ├── vad.py                 # Voice activity detection
│   │   └── wake_word.py           # Wake word detection
│   ├── memory/
│   │   ├── rag.py                 # RAG pipeline
│   │   ├── chroma_db.py           # ChromaDB vector store
│   │   ├── embeddings.py          # Embedding models
│   │   └── ingestion.py           # Document ingestion
│   ├── system/
│   │   ├── control.py             # OS control (volume, brightness, apps, screenshot)
│   │   ├── executor.py            # Sandboxed Python/Bash code execution
│   │   ├── files.py               # File search, read, organize
│   │   ├── browser.py             # Playwright browser automation
│   │   └── email_client.py        # SMTP/IMAP email
│   ├── agents/
│   │   ├── scheduler_agent.py     # APScheduler-based task scheduler
│   │   ├── task_decomposer.py     # Multi-step task breakdown
│   │   ├── research_agent.py      # Web research agent
│   │   ├── vision_agent.py        # Vision/image analysis
│   │   ├── code_agent.py          # Code generation agent
│   │   └── cad_agent.py           # CAD/design agent
│   └── iot/
│       ├── home_assistant.py      # Home Assistant REST API
│       ├── mqtt_client.py         # MQTT broker client
│       ├── nl_mapper.py           # NL → IoT command mapper
│       └── ws_listener.py         # HA WebSocket listener
├── static/
│   ├── index.html                 # 3-panel layout (history | HUD | chat), scripts v=8
│   ├── css/hud.css                # Full TRON-X styling
│   └── js/
│       ├── app.js                 # Boot sequence, event wiring, v=8
│       ├── chat.js                # Chat v3: streams silently, reveals with voice
│       ├── voice.js               # Voice v3: speakAndReveal(), chunked TTS
│       └── scene.js               # Three.js orb + state machine
├── config/
│   ├── models.json                # 104 models, 13 providers, 14 categories
│   ├── personas.json              # JARVIS/FRIDAY personality configs
│   └── settings.yaml              # App settings
├── memory/cache/sessions.json     # Persisted chat sessions
├── .env                           # Your actual keys (never commit)
├── .env.example                   # Template with all 13 provider keys documented
└── TRONX_MASTER_PROMPT.md         # This file
```

---

## What's Already Built & Working

### Voice System (Phase 1 — COMPLETE)
- **`voice.js` v3**: `speakAndReveal(text, persona, onStart, onEnd)`
  - Text buffered silently during streaming (shows "Composing...")
  - After stream complete → synthesises full text (NO 350-char cap)
  - `onStart` fires the instant audio begins → text + voice reveal simultaneously
  - Long text chunked at ~800 chars on sentence boundaries, pre-fetched in parallel
- **`chat.js` v3**: no early TTS trigger, no snippet cap, full response spoken
- **`tts.py`**: Kokoro-82M (local) → edge-tts (online) → pyttsx3 (offline SAPI)

### Model Registry (Phase 2 — COMPLETE)
**`config/models.json`** — 104 models, 13 providers:

| Provider | Models | Free? | Env Key |
|---|---|---|---|
| groq | 7 (llama-3.3-70b, 3.1-8b, gemma2-9b, mixtral...) | Yes | GROQ_API_KEY |
| cerebras | 3 (llama3.1-8b, 3.1-70b, 3.3-70b) | Yes | CEREBRAS_API_KEY |
| openrouter | 15 (:free models incl. DeepSeek-R1, Phi-4...) | Yes | OPENROUTER_API_KEY |
| gemini | 3 (gemini-2.0-flash 1M ctx, 1.5-flash...) | Yes | GEMINI_API_KEY |
| together_ai | 19 (DeepSeek-R1/V3, Qwen2.5-72B, Llama-405B...) | Paid | TOGETHER_API_KEY |
| fireworks_ai | 13 (Llama-3.3-70B, Qwen2.5-Coder, DeepSeek-R1...) | Paid | FIREWORKS_AI_API_KEY |
| deepinfra | 14 (Llama-3.3-70B, Qwen2.5-72B, Nemotron-70B...) | Paid | DEEPINFRA_API_KEY |
| mistral | 7 (open-mistral-nemo, codestral, mixtral-8x22b...) | Paid | MISTRAL_API_KEY |
| cohere | 4 (command-r-plus, command-r, command-light...) | Paid | COHERE_API_KEY |
| perplexity | 4 (sonar-pro w/ web search, sonar-reasoning...) | Paid | PERPLEXITYAI_API_KEY |
| deepseek | 3 (deepseek-chat/V3, deepseek-reasoner/R1, coder) | Paid | DEEPSEEK_API_KEY |
| huggingface | 5 (zephyr-7b, starcoder2, phi-3-mini...) | Free | HUGGINGFACE_API_KEY |
| ollama | 7 (local: llama3.2, codellama, qwen2.5...) | Local | none |

**14 routing categories** (intent → best model):
- `fast_chat` → groq/llama-3.3-70b-versatile
- `reasoning` → together_ai/DeepSeek-R1
- `coding` → together_ai/Qwen2.5-Coder-32B
- `vision` → gemini/gemini-2.0-flash
- `math` → together_ai/DeepSeek-R1
- `creative` → together_ai/Qwen2.5-72B
- `research` → perplexity/sonar-pro
- `academic` → cohere/command-r-plus
- `medical` → together_ai/DeepSeek-R1
- `long_context` → gemini/gemini-2.0-flash (1M ctx)
- `fast_edge` → cerebras/llama3.1-8b
- `iot` → groq/llama-3.1-8b-instant
- `system` → groq/llama-3.3-70b-versatile
- `cad` → together_ai/Qwen2.5-Coder-32B

**42 intent → category mappings** (e.g. "coding"/"debug"/"programming" → coding category)

### Router (`src/intelligence/router.py`)
- `SmartRouter` class with `HealthTracker` (circuit breaker) + `SlidingWindowRateLimiter`
- `_inject_api_keys()` pushes all 12 env vars into `os.environ` at startup
- `_filter_params()` strips unsupported params per provider (no more 400 errors)
- `_get_chain()` filters chain to only providers with keys configured
- `complete(messages, category, stream, **kwargs)` → failover across full chain

### GUI — 3-Panel Layout
```
[History Panel] | [HUD Visualiser] | [Chat Panel]
  180px         |    ~38%          |   remaining
```
- **History panel**: past sessions list, click to reload, ＋ new chat button
- **HUD**: Three.js orb, state machine (idle/thinking/speaking/listening), system info grid
- **Chat panel**: streaming bubbles, persona select, mute, clear
- **HUD shows**: active provider count (`X / 14`) + total models (104)
- **Cache version**: `v=8` on all script tags

### Known File-Sync Issue
Windows `Read`/`Edit`/`Write` tools and Linux bash mount (`/sessions/.../mnt/Tron_X/`) sometimes get out of sync (Linux shows truncated/stale file). **Always rewrite large files via bash `cat > ... << 'EOF'`**, then verify with `python3 -m py_compile` or `node --check`.

---

## Remaining 18 Phases — Full Details

### PHASE 3 — Smart Routing Upgrade
**Goal**: Improve intent→model selection with latency tracking, A/B testing, cost awareness.
**Files to modify**: `src/intelligence/router.py`, `config/models.json`
**What to build**:
- Track p50/p95 latency per model (rolling 100-request window), stored in `memory/cache/model_stats.json`
- Latency-aware selection: if primary model's p50 > 3s, prefer faster fallback
- Cost-aware routing: add `cost_per_1k_tokens` to models.json, track spend per session
- A/B test mode: `AB_TEST_MODELS=true` in .env → randomly split between top-2 models, log winner
- Expose `GET /api/models/stats` endpoint → latency/cost/usage per model
- Update HUD to show current model latency live

### PHASE 4 — System Control Agent (Windows)
**Goal**: JARVIS can control the Windows OS via natural language.
**Files**: `src/system/control.py` (exists, basic), `src/api/system.py` (exists, basic)
**What to build/extend**:
- PowerShell execution with safety whitelist (`src/system/powershell.py`)
  - Blocked: `rm -rf`, `format`, `del /f /s`, registry writes, netsh firewall
  - Allowed: process list, service status, network info, environment vars
- Process management: list processes, kill by name/PID, start app by name
- Service control: list Windows services, start/stop/restart
- `CMD_WHITELIST` in config — list of safe command prefixes
- Natural language → command via LLM: "what's eating my CPU?" → `tasklist /FI "STATUS eq RUNNING" /FO TABLE`
- New API routes: `POST /api/system/powershell`, `POST /api/system/process/kill`, `GET /api/system/processes`
- Wire into orchestrator intent `system` → calls system agent before LLM

### PHASE 5 — File System Agent
**Goal**: JARVIS can search, read, move, organize files on your PC.
**Files**: `src/system/files.py` (exists, basic), `src/api/system.py`
**What to build/extend**:
- Deep file search: by name (glob), content (grep), date range, size, type
- Batch rename with pattern (`*.jpg` → `photo_{n}.jpg`)
- Folder summary: list contents, sizes, newest/oldest files
- Duplicate detection: MD5 hash comparison across a directory tree
- Archive/zip: create zip of folder, extract zip
- Smart organize: sort Downloads by file type into subfolders
- Safety: all destructive ops require `confirm=True` param, log all actions
- New routes: `POST /api/files/search`, `POST /api/files/rename-batch`, `POST /api/files/organize`, `POST /api/files/archive`
- Wire into orchestrator: "find all PDFs from last week" → file agent

### PHASE 6 — Browser Automation (Playwright)
**Goal**: JARVIS can open a browser, navigate, click, scrape, screenshot.
**Files**: `src/system/browser.py` (exists, stub), new `src/agents/browser_agent.py`
**What to build**:
- `pip install playwright && playwright install chromium`
- `BrowserAgent` class: `open_url()`, `click(selector)`, `fill_form()`, `get_text()`, `screenshot()`, `scroll()`
- High-level actions: `search_google(query)`, `login_to(url, user, pass)`, `scrape_page(url)`
- Session management: persistent browser context, multiple tabs
- Screenshot → base64 → pass to vision model for "what do you see?"
- New routes: `POST /api/browser/navigate`, `POST /api/browser/action`, `POST /api/browser/screenshot`
- Wire into orchestrator intent `research` as optional enrichment

### PHASE 7 — Code Execution Sandbox
**Goal**: JARVIS can write + run code and show you results.
**Files**: `src/system/executor.py` (exists, Python only)
**What to build/extend**:
- Extend to support: Python, JavaScript (node), Bash (whitelist only)
- Resource limits: `subprocess` with timeout (15s default), max output 8000 chars
- Python sandbox: block `import os`, `import subprocess`, `open()` by pre-scanning AST
- Output capture: stdout + stderr + exit code + execution time
- Auto-install missing packages: detect `ModuleNotFoundError` → `pip install X --break-system-packages`
- Result formatting: code block with language tag, output block, error block
- New routes: `POST /api/execute/python`, `POST /api/execute/js`, `POST /api/execute/bash`
- Wire into orchestrator: after coding response, offer "Run this code?"
- HUD shows: last execution time + exit code

### PHASE 8 — Screen Capture + OCR + Vision
**Goal**: JARVIS can see your screen and analyse it.
**Files**: new `src/vision/screen.py`, extend `src/agents/vision_agent.py`
**What to build**:
- `pip install mss pytesseract easyocr pillow`
- Full screen capture via `mss` (fastest, cross-platform)
- Region capture: `capture_region(x, y, w, h)`
- OCR pipeline: Tesseract (fast) + EasyOCR fallback (better accuracy)
- Text extraction from screenshot → feed to LLM for analysis
- Window capture: capture specific app window by title
- "Screen reader" mode: describe what's on screen using vision model
- New routes: `POST /api/vision/screenshot`, `POST /api/vision/ocr`, `POST /api/vision/describe`
- Wire into voice: "JARVIS, what's on my screen?" → capture + describe

### PHASE 9 — Web Research Agent
**Goal**: JARVIS can do real multi-step web research with citations.
**Files**: `src/agents/research_agent.py` (exists, extend it)
**What to build**:
- Search providers (try in order): Brave Search API, Serper.dev, DuckDuckGo (free scrape)
- `BRAVE_API_KEY`, `SERPER_API_KEY` in .env + config.py
- Multi-hop research: search → read top 3 results → extract facts → search deeper if needed
- Citation tracker: every fact tagged with source URL
- Research report generator: structured markdown with citations
- Streaming research: yield progress events (searching... reading... synthesising...)
- New SSE endpoint: `POST /api/research/stream` → yields progress + final report
- Wire into orchestrator intent `research` (currently goes to Perplexity sonar)
- If Perplexity key available → use it. If not → use this custom pipeline.

### PHASE 10 — Multi-Agent Orchestration
**Goal**: JARVIS decomposes complex tasks and runs specialist sub-agents in parallel.
**Files**: `src/agents/task_decomposer.py` (exists), new `src/agents/coordinator.py`
**What to build**:
- `TaskCoordinator`: takes complex request → LLM decomposes into subtasks → assigns to agents
- Agent registry: `research_agent`, `code_agent`, `file_agent`, `browser_agent`, `vision_agent`
- Parallel execution: `asyncio.gather()` for independent subtasks
- Result aggregation: collect all agent outputs → LLM synthesises final answer
- Progress streaming: HUD shows "Agent 1: researching... Agent 2: coding..."
- New SSE endpoint: `POST /api/agents/coordinate/stream`
- Example: "Build me a Python scraper for product prices" →
  - Agent 1: research best scraping approach
  - Agent 2: write the code
  - Agent 3: test the code
  - Coordinator: combine into final response

### PHASE 11 — Calendar + Reminders
**Goal**: JARVIS knows your schedule and can manage it.
**Files**: new `src/agents/calendar_agent.py`
**What to build**:
- Windows Calendar via `win32com.client` (Outlook) or CalDAV (Google Calendar)
- `GOOGLE_CALENDAR_CREDS` in .env for OAuth2 Google Calendar
- Read events: "what's on my calendar tomorrow?"
- Create events: "schedule a meeting with John on Friday at 3pm"
- Reminders via Windows `winotify` notifications + voice announcement
- APScheduler integration (already have): schedule reminders as cron jobs
- New routes: `GET /api/calendar/events`, `POST /api/calendar/create`, `POST /api/calendar/remind`
- Wire into orchestrator intent `calendar`/`schedule`/`reminder`

### PHASE 12 — Email Agent
**Goal**: JARVIS reads and sends emails on your behalf.
**Files**: `src/system/email_client.py` (exists, basic SMTP)
**What to build/extend**:
- IMAP reader: connect to Gmail/Outlook, fetch inbox, search emails
- Thread summarisation: "summarise my unread emails"
- Smart compose: "draft an email to John about the project delay"
- Send with confirmation: always show draft + ask "send?" before sending
- Attachment handling: list, download, summarise attachments
- `.env` vars already: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS` + add `IMAP_HOST`
- New routes: `GET /api/email/inbox`, `POST /api/email/search`, `POST /api/email/send`, `POST /api/email/draft`

### PHASE 13 — Episodic Memory
**Goal**: JARVIS remembers who you are, your preferences, past decisions.
**Files**: new `src/memory/episodic.py`, extend `orchestrator.py`
**What to build**:
- `EpisodicMemory` class backed by ChromaDB collection `episodic`
- Auto-extract facts from every conversation: name, preferences, decisions, projects
- Extractor prompt: "Extract user facts from this conversation as JSON: name, preferences, ongoing_projects, decisions_made"
- Store with timestamp + session_id
- Recall at conversation start: inject top-5 relevant memories into system prompt
- User profile: `memory/cache/user_profile.json` — name, timezone, preferred tone, projects
- "Memory review" command: "JARVIS, what do you remember about me?"
- New routes: `GET /api/memory/profile`, `POST /api/memory/update`, `DELETE /api/memory/forget`
- Wire into orchestrator: `_build_messages()` prefixes relevant episodic memories

### PHASE 14 — Advanced Voice Pipeline
**Goal**: Sentence-level TTS streaming, ElevenLabs integration, emotion-aware voice.
**Files**: `src/voice/tts.py`, `static/js/voice.js`
**What to build**:
- Sentence-stream TTS: as text streams in, synthesise each sentence immediately → queue → play back-to-back (no waiting for full response)
- ElevenLabs integration: `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` in .env
  - JARVIS voice: `Adam` or custom clone
  - FRIDAY voice: `Rachel` or custom clone
- Emotion detection: analyse response text tone (excited/serious/concerned) → adjust TTS rate/pitch
- Voice speed control: `VOICE_SPEED` in .env (0.8–1.2)
- Multiple voice options in UI dropdown
- `/api/voice/tts` returns `duration_ms` so frontend can sync text reveal to exact voice length
- New `.env` vars: `ELEVENLABS_API_KEY`, `ELEVENLABS_JARVIS_VOICE_ID`, `ELEVENLABS_FRIDAY_VOICE_ID`, `VOICE_SPEED`

### PHASE 15 — Real-Time Data Feeds
**Goal**: JARVIS knows current weather, stocks, news without searching.
**Files**: new `src/agents/data_feeds.py`
**What to build**:
- Weather: OpenWeatherMap API (`OPENWEATHER_API_KEY`) → current + 5-day forecast
- Stocks: Alpha Vantage free tier (`ALPHAVANTAGE_API_KEY`) → price, change%, volume
- News: NewsAPI (`NEWSAPI_KEY`) → top headlines by category
- Crypto: CoinGecko free API (no key needed) → BTC/ETH/etc prices
- Caching: 10-minute TTL cache per feed to avoid rate limits
- HUD widget: rotating ticker at bottom showing weather/stock/news
- Wire into orchestrator: "what's the weather?" → data feed, not LLM hallucination
- New routes: `GET /api/data/weather?city=X`, `GET /api/data/stocks?symbol=X`, `GET /api/data/news?category=X`

### PHASE 16 — IoT Expansion (MQTT + more)
**Goal**: Full smart home control, more device protocols.
**Files**: `src/iot/mqtt_client.py` (exists), `src/iot/home_assistant.py` (exists)
**What to build/extend**:
- MQTT: publish/subscribe to any topic, device state tracking
- Custom device registry: `config/devices.json` — name, MQTT topic, type, room
- Scene control: "goodnight mode" → dims lights + locks doors + sets thermostat
- Automation triggers: "when I say 'I'm home' → turn on lights + TV"
- More HA entity types: covers, fans, cameras, doorbells, locks
- Device status in HUD: mini widget showing home state
- Voice commands: "JARVIS, set living room to 21 degrees"
- New `.env`: `MQTT_BROKER`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`
- New routes: `POST /api/iot/mqtt/publish`, `GET /api/iot/devices`, `POST /api/iot/scene`

### PHASE 17 — Analytics Dashboard
**Goal**: See exactly which models ran, how fast, and what they cost.
**Files**: new `src/api/analytics.py`, new `static/js/analytics.js`
**What to build**:
- Event logger: every LLM call → append to `memory/logs/analytics.jsonl`
  - Fields: timestamp, model, provider, category, tokens_in, tokens_out, latency_ms, cost_usd, session_id
- Cost calculator: per-provider pricing table in `config/pricing.json`
- Analytics API: `GET /api/analytics/summary?days=7` → total spend, requests, avg latency
- Analytics UI: new tab/page with charts (Chart.js)
  - Model usage pie chart
  - Latency bar chart per provider
  - Daily cost line chart
  - Token usage histogram
- HUD mini-stats: today's cost estimate + total requests

### PHASE 18 — Plugin System
**Goal**: Drop a .py file in `plugins/` → JARVIS loads it automatically.
**Files**: new `src/core/plugin_loader.py`
**What to build**:
- Plugin spec: each plugin is a class inheriting `TronXPlugin`
  - Methods: `register_intents()`, `register_routes(router)`, `on_message(text) -> Optional[str]`
  - Metadata: `name`, `version`, `description`, `author`
- Auto-loader: `PluginLoader` watches `plugins/` dir, imports on startup
- Hot-reload: FileSystemWatcher reloads plugin on change (dev mode)
- Plugin API: `GET /api/plugins` → list installed, `POST /api/plugins/reload`
- Example plugins in `plugins/examples/`: weather plugin, todo plugin, calculator plugin
- UI: plugin list in settings panel, enable/disable toggle
- Safety: plugins run in restricted namespace, no direct DB/session access

### PHASE 19 — Security Hardening
**Goal**: Auth, safe execution, key management.
**Files**: `src/core/config.py`, `src/main.py`, new `src/core/auth.py`
**What to build**:
- Optional PIN auth: `UI_PIN=1234` in .env → browser prompts for PIN on first load (localStorage token)
- JWT token: `POST /api/auth/login {pin}` → returns JWT → all API routes require `Authorization: Bearer <token>`
- API key vault: encrypt `.env` with machine key using `cryptography.fernet`
- Rate limiting per IP: `slowapi` middleware, 60 req/min default
- Command audit log: every system/file/browser action logged to `memory/logs/audit.jsonl`
- PowerShell/bash hardened whitelist: reviewed and tightened
- CORS: lock down to `localhost` only in production mode (`APP_ENV=production`)
- CSP headers on all HTML responses

### PHASE 20 — Production Hardening
**Goal**: Run reliably 24/7, auto-restart on crash, Docker support.
**Files**: new `Dockerfile`, `docker-compose.yml`, `scripts/start.bat`
**What to build**:
- `Dockerfile`: Python 3.11, copy src, `pip install -r requirements.txt`, `CMD uvicorn...`
- `docker-compose.yml`: tron-x service + optional ChromaDB service
- Windows auto-start: `scripts/install_service.bat` → NSSM service wrapper
- `scripts/start.bat`: one-click launcher (activates venv, starts uvicorn)
- Health check endpoint already exists at `GET /api/health`
- Structured JSON logging: `LOG_FORMAT=json` in .env → all logs as JSON for Loki/Datadog
- Graceful shutdown: already handled in `main.py` lifespan
- `.env` validation on startup: check required keys, warn on missing optional
- `requirements.txt` audit: pin all versions, split into `requirements.txt` + `requirements-dev.txt`
- Nginx config snippet: reverse proxy + SSL termination example

---

## Important Rules for All Future Phases

1. **One phase at a time** — complete, test, verify before starting next
2. **Always rewrite large files via bash heredoc** (`cat > file << 'EOF'`) — never use Edit tool on files > 100 lines; the Windows/Linux mount gets out of sync
3. **After every file write**: run `python3 -m py_compile` (Python) or `node --check` (JS)
4. **Bump cache version** on `static/index.html` script tags after any JS/CSS change (currently at `v=8`)
5. **Test the actual server** — always check server logs after restart, don't assume it works
6. **No proprietory/closed-source models** — 100% open source. Gemini is OK (free tier, open API). No GPT-4, no Claude API.
7. **Cross-check integration** — new agents must be wired into `orchestrator.py` intent routing and have API endpoints in `src/api/`

---

## How to Start Next Phase

Say: `proceed with phase X` — I will execute it completely end-to-end.
