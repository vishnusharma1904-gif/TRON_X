# TRON-X Project Context — Handoff Document

## What This Is

TRON-X is a FastAPI + Uvicorn backend with a Three.js HUD frontend. It is a personal AI assistant system for Windows with multi-agent capabilities, voice, vision, memory, and system control.

**Stack:** Python 3.11+, FastAPI, Uvicorn, LiteLLM (multi-provider LLM routing), ChromaDB (vector memory), Playwright (browser agent), APScheduler, SentenceTransformers.

**Project root:** `D:\Tron_X\`
**Entry point:** `src/main.py`
**Run command:** `uvicorn src.main:app --reload`

---

## Workflow Rules (Critical)

1. **Never skip verification** — every file gets `python3 -m py_compile` after writing.
2. **Unicode truncation bug** — files with `──` (U+2500) box-drawing chars cause the Edit tool to silently truncate. Always write via bash `cat > /tmp/file.py << 'ENDOFFILE'` then `cp` to destination. Never use Unicode box chars in code.
3. **Router prefix discipline** — all routers have prefix set at creation (`APIRouter(prefix="/api/x")`). Route decorators use relative paths (`"/endpoint"` not `"/api/x/endpoint"`).
4. **Blocking I/O** — all blocking calls wrapped with `asyncio.get_event_loop().run_in_executor(None, fn)`.
5. **Confirm guard** — all destructive ops require `confirm: bool = False`; return error dict if False.
6. **Auto-install pattern** — on `ModuleNotFoundError`, run `pip install pkg --break-system-packages --quiet`, retry once.
7. **DeepSeek workflow** — for complex phases, Claude writes a detailed spec prompt → user pastes to DeepSeek v4 Pro → user pastes code back → Claude verifies + deploys. Claude writes simpler phases directly.

---

## Bash Path Mapping (Cowork Session)

| Windows path | Bash path |
|---|---|
| `D:\Tron_X\` | `/sessions/friendly-jolly-carson/mnt/Tron_X/` |
| outputs dir | `/sessions/friendly-jolly-carson/mnt/outputs/` |
| uploads dir | `/sessions/friendly-jolly-carson/mnt/uploads/` |

---

## Architecture Overview

```
src/
  main.py                    # FastAPI app, all routers registered here
  core/
    config.py                # Pydantic Settings, reads .env
    logger.py                # log singleton
    exceptions.py
  intelligence/
    router.py                # LiteLLM multi-provider router + LatencyTracker + ABTestManager
    orchestrator.py          # Main chat orchestrator, intent routing, RAG injection
    intent.py                # Intent classifier
    persona.py               # Persona system prompts (jarvis, friday, etc.)
    prompts.py
    cot.py                   # Chain-of-thought
  api/
    health.py                # /api/health, /api/latency/stats, /api/ab-test/*
    chat.py                  # /api/chat
    memory.py                # /api/memory (ingest, search, stats)
    voice.py                 # /api/voice
    system.py                # /api/system (OS, files, browser, email, code, vision)
    iot.py                   # /api/iot
    agents.py                # /api/agents (research, coordinator, schedule, pipeline)
    calendar.py              # /api/calendar
    email.py                 # /api/email
    episodic.py              # /api/memory/episodic
  agents/
    research_agent.py        # ResearchAgent (v1) + ResearchAgentV2 (Phase 9)
    browser_agent.py         # BrowserAgent singleton (Playwright)
    coordinator.py           # TaskCoordinator with agent registry (Phase 10)
    calendar_agent.py        # Google Calendar OAuth2 (Phase 11)
    reminder_agent.py        # winotify reminders (Phase 11)
    email_agent.py           # IMAP reader + LLM summarization (Phase 12)
    task_decomposer.py       # LLM-planned multi-agent pipeline
    scheduler_agent.py       # APScheduler wrapper
    code_agent.py
    cad_agent.py
    vision_agent.py
  system/
    control.py               # OS control: volume, brightness, processes, services
    powershell.py            # Safe PowerShell runner
    files.py                 # File ops + batch + archives
    executor.py              # Python/JS/Bash sandboxed execution
    browser.py               # Simple browser (non-Playwright)
    email_client.py          # SMTP sender
  memory/
    chroma_db.py             # ChromaDB manager (4 collections)
    embeddings.py            # SentenceTransformers all-MiniLM-L6-v2
    rag.py                   # RAG pipeline with MMR reranking
    episodic_memory.py       # EpisodicMemoryAgent (Phase 13)
    ingestion.py             # Document ingestion
    supabase_client.py
  vision/
    screen.py                # Screenshot, OCR (Tesseract+EasyOCR), vision description
  voice/
    stt.py / tts.py / vad.py / wake_word.py
  iot/
    home_assistant.py / mqtt_client.py / nl_mapper.py / ws_listener.py
```

---

## Config (.env keys)

```
# LLM Providers (LiteLLM)
OPENAI_API_KEY, ANTHROPIC_API_KEY, GOOGLE_API_KEY, GROQ_API_KEY
TOGETHER_API_KEY, FIREWORKS_AI_API_KEY, DEEPINFRA_API_KEY
MISTRAL_API_KEY, COHERE_API_KEY, PERPLEXITYAI_API_KEY, DEEPSEEK_API_KEY
HUGGINGFACE_API_KEY
OLLAMA_ENABLED, OLLAMA_BASE_URL, OLLAMA_MODEL

# Storage
SUPABASE_URL, SUPABASE_ANON_KEY

# IoT
HA_URL, HA_TOKEN

# Email
SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM
IMAP_HOST, IMAP_PORT (993), IMAP_USER, IMAP_PASS, IMAP_SSL (true)

# Search (Phase 9)
BRAVE_API_KEY, SERPER_API_KEY   # DuckDuckGo always available as fallback

# Calendar (Phase 11)
GOOGLE_CREDENTIALS_PATH   # default: ~/.tronx/gcal_credentials.json
```

---

## Completed Phases

### Phase 4 — System Control Agent
**Files:** `src/system/powershell.py` (new), `src/system/control.py` (extended), `src/api/system.py` (extended)

`powershell.py`:
- `CMD_WHITELIST` (prefix list) + `BLOCKED_PATTERNS` (17 regexes, `re.IGNORECASE`)
- `safety_scan(command)` → `(bool, str)`
- `run_powershell(command, timeout=15)` → dict
- `nl_to_powershell(query, router)` → dict

`control.py` additions:
- `mute()` — zero params, returns dict
- `list_processes(sort_by="cpu")` — executor-wrapped psutil, top 30
- `kill_process(identifier)` — protected names guard: `{"system","smss.exe","csrss.exe","wininit.exe","winlogon.exe","lsass.exe","svchost.exe"}`
- `start_process(path_or_name)` — `_APP_MAP_WINDOWS` dict (11 entries: chrome, firefox, notepad, explorer, terminal, cmd, calculator, paint, task manager, vscode, spotify)
- `list_services(state_filter="all")` — Windows-only, PS JSON output
- `service_action(service_name, action)` — critical services guard: `{"windefend","eventlog","wuauserv","lanmanserver"}`

API routes (all relative, prefix `/api/system`):
`POST /powershell`, `POST /nl-command`, `GET /processes`, `POST /process/kill`, `POST /process/start`, `GET /services`, `POST /service/{name}/{action}`

---

### Phase 5 — File System Agent
**File:** `src/system/files.py` (extended). Added imports: `hashlib`, `fnmatch`, `zipfile`.

- `folder_summary(path)` — size, file count, type breakdown
- `find_duplicates(root, extensions=None)` — MD5 hash, groups 2+ identical files
- `rename_batch(root, pattern, template, confirm=False)` — `{n}`, `{name}`, `{ext}` tokens
- `organize_folder(root, confirm=False)` — 7 categories (Images, Documents, Videos, Audio, Archives, Code, Other)
- `create_archive(sources, dest, confirm=False)` — zip
- `extract_archive(src, dest, confirm=False)` — zip/tar

API routes: `POST /files/summary`, `POST /files/duplicates`, `POST /files/rename-batch`, `POST /files/organize`, `POST /files/archive`, `POST /files/extract`

---

### Phase 6 — Browser Agent (Playwright Singleton)
**File:** `src/agents/browser_agent.py` (new). Written via DeepSeek spec.

Key design:
```python
_lock: asyncio.Lock | None = None
def _get_lock() -> asyncio.Lock: ...   # lazy factory — NOT class variable (breaks before event loop)

class BrowserAgent:
    _instance: BrowserAgent | None = None

    @classmethod
    async def get(cls) -> BrowserAgent: ...   # singleton, thread-safe via _get_lock()
    async def start(self) -> None: ...        # launches Playwright, Chromium, persistent context
    async def stop(self) -> None: ...         # closes all, resets _instance = None
```
Actions: `navigate`, `get_text`, `click`, `fill`, `scroll`, `screenshot`, `scrape`, `search_google`, `action` (dispatcher). Each: new_page → action → page.close() in `finally`.

API routes (in `system.py`): `POST /browser/navigate`, `POST /browser/action`, `POST /browser/screenshot/v2`, `DELETE /browser/session`

---

### Phase 7 — Code Execution Sandbox
**File:** `src/system/executor.py` (extended)

```python
_BLOCKED_MODULES = frozenset({"os","subprocess","socket","ctypes","shutil","urllib",
                               "http","ftplib","smtplib","importlib","pty",
                               "multiprocessing","builtins","pickle","shelve"})
_BASH_WHITELIST = {"echo","ls","cat","pwd","date","python3","node","grep",...}  # 22 commands
_BASH_BLOCKED = re.compile(r"(rm\s|sudo|chmod|...)", re.IGNORECASE)
```

- `_ast_scan(code)` — walks AST, blocks forbidden imports + `__import__()`, `exec()`, `eval()`
- `_auto_install(package)` — `pip install --break-system-packages`, retry once
- `execute_python_safe(code, timeout=15, auto_install=True)` — wall_ms timing
- `execute_js(code, timeout=15)` — `node -e`, blocks dangerous `require()`
- `execute_bash(code, timeout=15)` — whitelist first token

API routes: `POST /execute/python`, `POST /execute/js`, `POST /execute/bash`

---

### Phase 8 — Screen Capture + OCR + Vision
**File:** `src/vision/screen.py` (new)

- `capture_screen(save_path, region, monitor=1, return_base64=False)` — mss
- `capture_window(title, save_path, return_base64=False)` — pygetwindow
- `ocr_image(path, engine="auto")` — Tesseract fast path → EasyOCR fallback
- `ocr_screen(region, engine="auto")`
- `describe_screen(region, prompt, return_base64=False)` — vision LLM via router
- `describe_image(path, prompt)` — vision LLM

API routes: `POST /vision/screenshot`, `POST /vision/screenshot/window`, `POST /vision/ocr`, `POST /vision/ocr/screen`, `POST /vision/describe`, `POST /vision/describe/image`

---

### Phase 9 — Web Research Agent V2
**File:** `src/agents/research_agent.py` (appended `ResearchAgentV2`). Written via DeepSeek spec.

```python
class ResearchAgentV2:
    MAX_SEARCH_RESULTS = 5
    MAX_PAGE_CHARS     = 3000
    MAX_HOPS           = 2
```

Provider cascade (in `_search()`): Perplexity fast-path (if `PERPLEXITYAI_API_KEY` set, skips search) → Brave → Serper → DuckDuckGo (always available).

- `_search_brave/serper/ddg(query)` — each returns `[{title, url, snippet}]`
- `_fetch_one(url)` — strips `<script>/<style>`, regex HTML strip, caps at 3000 chars
- `_fetch_pages(urls)` — `asyncio.gather` top 3
- `_run_perplexity(query)` — `preferred_model="perplexity/sonar-pro"`
- `run(query, max_hops=1)` — Perplexity OR cascade → fetch → optional 2nd hop (LLM generates follow-up query) → synthesise with citations `[1][2][3]`
- `stream(query, max_hops=1)` — `AsyncGenerator[str, None]`, yields SSE events

SSE event format: `f"data: {json.dumps(payload)}\n\n"`
Event types: `progress` (step, message), `result` (data), `error` (message)
Progress steps: `search`, `fetch`, `reading`, `deep_search`, `synthesise`, `perplexity`

**`src/api/agents.py`** additions:
```python
class ResearchV2Req(BaseModel):
    query: str
    max_hops: int = Field(default=1, ge=1, le=2)

POST /api/agents/research/v2     # non-streaming
POST /api/agents/research/stream  # StreamingResponse, media_type="text/event-stream"
                                  # headers: Cache-Control: no-cache, X-Accel-Buffering: no
```

---

### Phase 10 — Multi-Agent Orchestration (TaskCoordinator)
**File:** `src/agents/coordinator.py` (new)

Also fixed `src/agents/task_decomposer.py` — was truncated mid-function, missing `_synthesise()` call and return statement.

Registry pattern:
```python
@register_agent("name", "description")
async def _agent_fn(payload: dict) -> dict: ...
```

11 registered agents: `research_v2`, `research`, `python`, `js`, `bash`, `browser_scrape`, `screenshot`, `ocr_screen`, `describe_screen`, `system_info`, `processes`

`TaskCoordinator` static methods:
- `registry()` — list all registered agents
- `run_one(agent_name, payload)` — single agent with ms timing
- `run_parallel(tasks)` — `asyncio.gather` all tasks simultaneously
- `run_sequential(tasks, share_context=False)` — one at a time; `share_context=True` injects each result into next task's payload as `previous_result`
- `stream_parallel(tasks)` — `asyncio.wait(FIRST_COMPLETED)` loop, emits events as each finishes

SSE event types: `agent_start`, `agent_result`, `agent_error`, `done`

**`src/api/agents.py`** additions:
```
GET  /api/agents/coordinate/registry   # list agents
POST /api/agents/coordinate/single     # run one agent
POST /api/agents/coordinate            # mode: parallel|sequential, share_context bool
POST /api/agents/coordinate/stream     # SSE parallel dispatch
```

---

### Phase 11 — Calendar + Reminders
**Files:** `src/agents/calendar_agent.py` (new), `src/agents/reminder_agent.py` (new), `src/api/calendar.py` (new)

**`CalendarAgent`** — Google Calendar OAuth2:
- Token cached at `~/.tronx/gcal_token.json`
- Credentials JSON from Google Cloud Console at `~/.tronx/gcal_credentials.json` or `GOOGLE_CREDENTIALS_PATH` env var
- Auto-installs `google-api-python-client google-auth google-auth-oauthlib` on first use
- All Google API calls wrapped in `run_in_executor`
- Methods: `auth_status`, `is_authenticated`, `list_events(days, max_results, calendar_id)`, `create_event(title, start, end, description, location, attendees, all_day)`, `update_event(event_id, **kwargs)`, `delete_event(event_id)`, `find_free_slots(date, duration_minutes, work_start=9, work_end=18)`, `list_calendars`

**`ReminderAgent`** (singleton via `get_reminder_agent()`):
- winotify for Windows toast notifications; auto-installs on Windows; console-log fallback elsewhere
- Reminders persisted to `~/.tronx/reminders.json`, reloaded on startup (pending only)
- `set_reminder(message, fire_at=None, delay_seconds=None, title, reminder_id)` — `asyncio.create_task(_fire_async())` with `asyncio.sleep(delay)`
- `set_reminder_nl(message, when_nl)` — passes through `SchedulerAgent.parse_nl_schedule()` → delay_seconds
- `list_reminders(include_fired)`, `cancel_reminder(id)`, `fire_now(id)`

**`src/api/calendar.py`** — 13 routes at `/api/calendar`:
Calendar: `GET /auth/status`, `POST /auth/connect`, `GET /events`, `POST /events`, `PATCH /events`, `DELETE /events/{id}`, `POST /free-slots`, `GET /calendars`
Reminders: `POST /reminders`, `POST /reminders/nl`, `GET /reminders`, `DELETE /reminders/{id}`, `POST /reminders/{id}/fire`

---

### Phase 12 — Email Agent (IMAP)
**Files:** `src/agents/email_agent.py` (new), `src/api/email.py` (new)
**Config additions to `src/core/config.py`:** `IMAP_HOST`, `IMAP_PORT` (993), `IMAP_USER`, `IMAP_PASS`, `IMAP_SSL` (true)

All stdlib — `imaplib`, `email`. No extra dependencies.

Key internals:
- `_decode_mime_words(raw)` — RFC 2047 encoded headers
- `_extract_body(msg, prefer_plain=True)` — walks MIME tree, prefers plain → HTML fallback, strips HTML tags
- `_strip_html(html)` — removes `<script>/<style>`, strips all tags
- `_connect()` → `IMAP4_SSL` or `IMAP4` based on `imap_ssl` setting
- All IMAP calls wrapped in `run_in_executor`

`EmailAgent` methods:
- `connection_status()`, `list_folders()`
- `fetch_emails(folder, limit, unread_only)` — headers + 300-char snippet, newest first
- `read_email(uid, folder)` — full body
- `search_emails(query, folder, limit)` — raw IMAP SEARCH passthrough (`FROM "x"`, `SUBJECT "y"`, `SINCE "01-Jun-2026"`, `UNSEEN`, etc.)
- `mark_read(uid, folder)`, `mark_unread(uid, folder)`
- `delete_email(uid, folder, trash_folder, confirm=False)` — tries RFC 6851 MOVE, falls back to COPY+DELETE+expunge
- `summarize_inbox(folder, limit, unread_only, persona)` — fetch → LLM digest with action items
- `summarize_thread(uids, folder, persona)` — fetch multiple → LLM with decisions/timeline/open questions
- `reply_draft(uid, instructions, folder, tone, persona)` — reads original → LLM draft, user reviews then calls SMTP send

**`src/api/email.py`** — 11 routes at `/api/email`: `/status`, `/folders`, `/fetch`, `/read`, `/search`, `/mark/read`, `/mark/unread`, `/delete`, `/summarize/inbox`, `/summarize/thread`, `/reply/draft`

---

### Phase 13 — Episodic Memory (ChromaDB)
**Files:** `src/memory/episodic_memory.py` (new), `src/api/episodic.py` (new)
**Modified:** `src/memory/chroma_db.py` — added `COL_EPISODES = "episodes"` (4th collection, auto-created on startup)

Episode schema stored as ChromaDB metadata:
```
episode_id, session_id, user_msg (500 chars), assistant (500 chars),
summary, topic, entities (comma-separated), emotion (positive|neutral|negative),
timestamp (float unix epoch), date (YYYY-MM-DD)
```
The **embedded text** = `f"{summary} | topic: {topic} | entities: {entities}"` — more semantic than raw message.

`_llm_extract(user_msg, assistant_reply)` — async, asks LLM to return JSON `{summary, topic, entities, emotion}`. Falls back gracefully on parse failure.

`EpisodicMemoryAgent` methods:
- `remember(user_msg, assistant_reply, session_id, auto_extract=True, topic, entities, emotion)` — stores episode, returns `{stored, episode_id, summary, topic, ...}`
- `recall(query, top_k=5, days=None, session_id=None, topic=None, min_score=0.30)` — semantic search with ChromaDB `$and` where filter for time/session/topic
- `daily_summary(date=None, persona)` — fetches all episodes by `date` metadata field → LLM recap
- `period_summary(days=7, persona)` — groups episodes by date → LLM thematic summary
- `list_episodes(days, session_id, limit)` — raw metadata listing
- `stats()` — total count, earliest/latest date, top-10 topics
- `forget_episode(episode_id)`, `forget_session(session_id)`, `forget_before(days, confirm=False)`

**`src/api/episodic.py`** — 9 routes at `/api/memory/episodic`: `POST /remember`, `POST /recall`, `GET /episodes`, `GET /stats`, `GET /summary/daily`, `GET /summary/period`, `DELETE /episode/{id}`, `DELETE /session`, `DELETE /before`

---

## `src/main.py` — Router Registration (current state)

```python
from src.api.health   import router as health_router
from src.api.chat     import router as chat_router
from src.api.memory   import router as memory_router
from src.api.voice    import router as voice_router
from src.api.system   import router as system_router
from src.api.iot      import router as iot_router
from src.api.agents   import router as agents_router
from src.api.calendar import router as calendar_router
from src.api.email    import router as email_router
from src.api.episodic import router as episodic_router

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(memory_router)
app.include_router(voice_router)
app.include_router(system_router)
app.include_router(iot_router)
app.include_router(agents_router)
app.include_router(calendar_router)
app.include_router(email_router)
app.include_router(episodic_router)
```

---

## `src/api/system.py` — Route Summary (429 lines)

| Phase | Routes |
|---|---|
| Original | `POST /volume`, `POST /mute`, `POST /brightness`, `POST /app/open`, `POST /app/close`, `POST /screenshot`, `GET /info` |
| Original | `POST /files/search`, `POST /files/read`, `POST /files/list`, `POST /files/rename`, `POST /files/copy`, `POST /files/delete`, `POST /files/create`, `GET /files/disk` |
| Original | `POST /browser/open`, `POST /browser/screenshot`, `POST /browser/search` |
| Original | `POST /email/send`, `POST /email/draft` |
| Original | `POST /exec` |
| Phase 4 | `POST /powershell`, `POST /nl-command`, `GET /processes`, `POST /process/kill`, `POST /process/start`, `GET /services`, `POST /service/{name}/{action}` |
| Phase 5 | `POST /files/summary`, `POST /files/duplicates`, `POST /files/rename-batch`, `POST /files/organize`, `POST /files/archive`, `POST /files/extract` |
| Phase 6 | `POST /browser/navigate`, `POST /browser/action`, `POST /browser/screenshot/v2`, `DELETE /browser/session` |
| Phase 7 | `POST /execute/python`, `POST /execute/js`, `POST /execute/bash` |
| Phase 8 | `POST /vision/screenshot`, `POST /vision/screenshot/window`, `POST /vision/ocr`, `POST /vision/ocr/screen`, `POST /vision/describe`, `POST /vision/describe/image` |

---

## `src/api/agents.py` — Route Summary (276 lines)

| Route | Description |
|---|---|
| `POST /api/agents/run` | LLM-planned multi-agent pipeline (task_decomposer) |
| `POST /api/agents/research` | ResearchAgent v1 |
| `POST /api/agents/code` | CodeAgent |
| `GET /api/agents/schedule` | List scheduled jobs |
| `POST /api/agents/schedule` | Add scheduled job (NL schedule parsing) |
| `DELETE /api/agents/schedule/{job_id}` | Remove job |
| `POST /api/agents/schedule/briefing` | Register daily briefing |
| `POST /api/agents/research/v2` | ResearchAgentV2 (non-streaming) |
| `POST /api/agents/research/stream` | ResearchAgentV2 SSE stream |
| `GET /api/agents/coordinate/registry` | List registered coordinator agents |
| `POST /api/agents/coordinate/single` | Run one agent |
| `POST /api/agents/coordinate` | Run multiple agents (parallel or sequential) |
| `POST /api/agents/coordinate/stream` | SSE parallel dispatch |

---

## Intelligence Layer

**`src/intelligence/router.py`** — `LiteLLMRouter` wrapping multiple providers:
- `LatencyTracker` — tracks per-model p50/p95 latency
- `ABTestManager` — routes % of traffic to variant models
- Intent-to-model mapping: `fast_chat` → Groq/Haiku, `research` → GPT-4o/Sonnet, `reasoning` → o1/Opus, `vision` → GPT-4o-vision, `code` → Codex/DeepSeek-Coder

**`src/intelligence/orchestrator.py`** — main `chat()` entrypoint:
- RAG context injection (if relevant memories found)
- Intent classification
- Per-intent preferred model hints
- Session management

---

## Memory Layer

**ChromaDB collections** (`memory/chroma/`):
- `conversations` — chat history embeddings
- `documents` — ingested PDFs, notes, web pages
- `knowledge` — manually added facts
- `episodes` — episodic memory (Phase 13)

**Embeddings:** SentenceTransformers `all-MiniLM-L6-v2`, 384-dim, CPU-optimised, cached via `@lru_cache`.

**RAG pipeline:** semantic search → MMR reranking (balances relevance + diversity, λ=0.5) → context injection into system prompt.

---

## Key Patterns to Preserve

```python
# SSE streaming (all streaming endpoints)
def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"

return StreamingResponse(
    generator(),
    media_type="text/event-stream",
    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
)

# Blocking I/O
result = await asyncio.get_event_loop().run_in_executor(None, blocking_fn)

# Confirm guard on destructive ops
if not confirm:
    return {"error": "Set confirm=True to proceed", "preview": ...}

# Auto-install
try:
    import package
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "package",
                    "--break-system-packages", "--quiet"], check=True)
    import package
```

---

## Remaining Phases

| Phase | Description | Complexity |
|---|---|---|
| **14** | Advanced Voice Pipeline — sentence-stream TTS, ElevenLabs integration | Medium — write directly |
| **15** | Real-Time Data Feeds — weather, stocks, news, crypto | Medium — write directly |
| **16** | IoT Expansion — MQTT broker, Home Assistant scenes, device grouping | Medium — write directly |
| **17** | Analytics Dashboard — usage stats, agent call counts, latency charts | Medium — write directly |
| **18** | Plugin System — dynamic agent loading from JSON manifests | High — consider DeepSeek |
| **19** | HUD Frontend — Three.js UI panels wired to API | High — DeepSeek |
| **20** | Production Hardening — auth, rate limiting, Docker, systemd service | Medium — write directly |

---

## Known Bugs Fixed This Session

1. **`task_decomposer.py` truncated** — `run_agent_pipeline()` was missing synthesis step and return. Fixed by appending the missing lines.
2. **`system.py` unclosed paren at line 196** — `compose_draft(` without closing paren. Fixed via full bash rewrite.
3. **`powershell.py` never deployed** — approved in previous session but never written to disk. Deployed from uploads.

---

## How to Start New Chat

Paste this entire document. Then say:

> "Continue TRON-X development. Phases 1–13 are complete. Start Phase 14: Advanced Voice Pipeline."
