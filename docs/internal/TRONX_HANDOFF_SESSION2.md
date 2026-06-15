# TRON-X Project — Session 2 Handoff Document
## Phases 14–20 Complete · All 20 Phases Shipped

---

## Quick Start

```bash
cd D:\Tron_X
uvicorn src.main:app --reload
# Open http://127.0.0.1:8000  →  redirects to HUD
```

---

## What Was Built This Session (Phases 14–20)

### Phase 14 — Advanced Voice Pipeline
- **`src/voice/tts.py`** — Full rewrite. Provider chain: ElevenLabs → Kokoro-82M → edge-tts → pyttsx3.
  - `synthesize_stream()` async generator for sentence-level streaming
  - Sentence splitter regex: `_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')`
  - Min sentence length: 8 chars before synthesis fires
- **`src/api/voice.py`** — 7 routes at `/api/voice`:
  - `POST /api/voice/stream` — SSE round-trip (LLM → TTS in one call)
  - `POST /api/voice/tts/stream` — TTS-only sentence streaming
  - `GET  /api/voice/elevenlabs/voices` — list available voices
  - Streaming uses asyncio Queue producer/consumer pattern
  - SSE event types: `transcript`, `audio_chunk`, `done`, `error`

### Phase 15 — Real-Time Data Feeds
- **`src/feeds/`** package — 4 modules, all zero-config-capable:
  - `weather.py` — OWM primary + wttr.in fallback (TTL 600s). **Fix applied: `r.json(content_type=None)` so aiohttp accepts wttr.in's text/plain response.**
  - `stocks.py` — yfinance (TTL 60s quotes / 300s history)
  - `news.py` — NewsAPI primary + Google News RSS fallback (TTL 900s)
  - `crypto.py` — CoinGecko only, no key needed (TTL 120s prices / 300s market). Symbol map: btc→bitcoin, eth→ethereum, etc.
- **`src/api/feeds.py`** — 14 routes at `/api/feeds`
- **`src/core/config.py`** — Added: `openweather_api_key`, `newsapi_key`, `alpha_vantage_key`

### Phase 16 — IoT Expansion
- **`src/iot/mqtt_client.py`** — Added: `_history` ring buffer (maxlen=50/topic), `unsubscribe()`, `list_topics()`, `get_topic_history()`, `stats()`
- **`src/iot/home_assistant.py`** — Added scenes, scripts, automations CRUD; `home_summary()` extended
- **`src/iot/device_groups.py`** — NEW. `DeviceGroupManager` persists to `~/.tronx/device_groups.json`. Concurrent fan-out via `asyncio.gather()`. Methods: create/update/delete/list/control/on/off/toggle/status
- **`src/api/iot.py`** — 41 total routes. New: MQTT (7), Scenes (4), Scripts (2), Automations (4), Device Groups (9)

### Phase 17 — Analytics Dashboard
- **`src/analytics/collector.py`** — `AnalyticsCollector` with aiosqlite at `~/.tronx/analytics.db`. 4 tables: `requests`, `chat_events`, `agent_events`, `error_events`. All writes fire-and-forget via `asyncio.create_task()`. Errors suppressed.
- **`src/analytics/middleware.py`** — `add_analytics_middleware(app)` — HTTP middleware, skips `/api/analytics` and `/static`.
- **`src/api/analytics.py`** — 8 endpoints at `/api/analytics`: summary, chat, agents, endpoints, models, errors, timeline, DELETE reset (confirm guard)
- **`src/api/chat.py`** — `_record_chat()` hook fires after every chat + vision call
- **`src/api/agents.py`** — `_record_agent()` hook fires after `coordinate/single` and `coordinate` (all modes)

### Phase 18 — Plugin System
- **`src/plugins/plugin_manifest.py`** — `PluginManifest` Pydantic schema: name, version, entry_module, agent_class, capabilities, intent_keywords, config_schema, requires
- **`src/plugins/plugin_registry.py`** — `PluginRegistry` singleton. Scans `~/.tronx/plugins/`. Uses `importlib` for dynamic loading. Auto-installs pip requirements. Persists enable/disable to `manifest.json`. Singleton via `get_registry()`.
- **`src/api/plugins.py`** — 9 endpoints at `/api/plugins`: list, capabilities, get, scan, reload, enable, disable, unload, run. Each mutating call syncs newly loaded plugins into `coordinator._REGISTRY` with `"fn"` key.
- **`src/main.py`** — auto-scans plugins at startup via `await get_registry().scan()`

**Plugin directory layout:**
```
~/.tronx/plugins/
    my_plugin/
        manifest.json    ← PluginManifest schema
        __init__.py      ← must export agent_class
```

**manifest.json example:**
```json
{
  "name": "weather_plugin",
  "version": "1.0.0",
  "entry_module": "weather_plugin",
  "agent_class": "WeatherAgent",
  "capabilities": ["weather"],
  "enabled": true
}
```

### Phase 19 — HUD Frontend
- **`static/index.html`** — 6-panel grid layout. Three.js canvas background + scanlines + corner decorations.
- **`static/js/hud.js`** — Three.js scene: wave-rippling particle grid (40×25), 3 rotating torus rings (cyan + orange), pulsing wireframe hex, slow camera drift.
- **`static/js/panels.js`** — All 6 panels wired to API:
  - **COMM CHANNEL** — SSE streaming chat. Persona switcher (JARVIS/FRIDAY).
  - **SYSTEM MONITOR** — Calls `/api/system/info`. Fields: `cpu_percent`, `ram_used_pct`, `disk_used_pct`. Polls every 4s.
  - **ANALYTICS** — Calls `/api/analytics/summary` every 8s.
  - **LIVE FEEDS** — Tabbed (Weather/Crypto/Stocks/News). Query input + FETCH button.
  - **IOT CONTROL** — Device list with inline ON/OFF toggle. MQTT topic list. Polls every 10s.
  - **AGENTS & PLUGINS** — Full coordinator registry + plugin list. Quick Run → `/api/agents/coordinate/single`.
- **`static/css/hud.css`** — Cyberpunk dark theme. CSS vars: `--cyan: #00e5ff`, `--orange: #ff6600`, `--dark: #000510`.
- **`src/main.py`** — Root `GET /` redirects to `/static/index.html`.

### Phase 20 — Production Hardening
- **`src/core/auth.py`** — `AuthMiddleware` + `require_api_key` FastAPI dependency. `secrets.compare_digest()` constant-time comparison. Reads `API_KEYS` (comma-separated) from `.env`. Off by default (`AUTH_ENABLED=false`).
- **`src/core/ratelimit.py`** — `RateLimitMiddleware`. Sliding-window per-IP + per-API-key. Returns 429 with `Retry-After` + `X-RateLimit-*` headers. `rate_limit_stats()` for monitoring. Off by default.
- **`src/core/config.py`** — Added: `auth_enabled`, `api_keys`, `auth_skip_paths`, `rate_limit_enabled`, `rate_limit_rpm`, `rate_limit_skip_paths`
- **`Dockerfile`** — Multi-stage build. Non-root `tronx` user. uvloop + httptools. Health check included.
- **`docker-compose.yml`** — Persistent volume for ChromaDB + analytics DB + plugins. JSON log rotation.
- **`deploy/tronx.service`** — systemd unit. `Restart=on-failure`. `ProtectSystem=strict`. 2 GB RAM / 200% CPU limits.
- **`.env.example`** — All keys from all 20 phases documented with source URLs.

**Middleware stack order in main.py (outermost → innermost):**
```
AuthMiddleware          ← outermost gate (Phase 20)
RateLimitMiddleware     ← 429 fires before auth overhead (Phase 20)
AnalyticsMiddleware     ← fire-and-forget recording (Phase 17)
_no_cache_static        ← Cache-Control for /static/*.js|css
add_timing              ← X-Response-Time header
CORSMiddleware          ← innermost
```

---

## Complete File Tree (all phases)

```
D:\Tron_X\
├── .env                        ← secrets (gitignored)
├── .env.example                ← template with all keys documented
├── Dockerfile                  ← Phase 20
├── docker-compose.yml          ← Phase 20
├── requirements.txt
├── deploy/
│   └── tronx.service           ← systemd unit (Phase 20)
├── static/
│   ├── index.html              ← Phase 19 HUD entry point
│   ├── css/hud.css             ← Phase 19
│   └── js/
│       ├── hud.js              ← Phase 19 Three.js background
│       └── panels.js           ← Phase 19 panel controllers
└── src/
    ├── main.py                 ← all routers + all middleware
    ├── core/
    │   ├── config.py           ← Pydantic Settings (all phases)
    │   ├── auth.py             ← Phase 20
    │   ├── ratelimit.py        ← Phase 20
    │   ├── logger.py
    │   └── exceptions.py
    ├── intelligence/
    │   ├── router.py           ← LiteLLM SmartRouter + LatencyTracker
    │   ├── orchestrator.py     ← main chat + intent routing
    │   ├── intent.py
    │   ├── persona.py
    │   ├── prompts.py
    │   └── cot.py
    ├── api/
    │   ├── health.py           ← /api/health
    │   ├── chat.py             ← /api/chat (+ analytics hook)
    │   ├── memory.py           ← /api/memory
    │   ├── voice.py            ← /api/voice (Phase 14)
    │   ├── system.py           ← /api/system
    │   ├── iot.py              ← /api/iot (Phase 16, 41 routes)
    │   ├── agents.py           ← /api/agents (+ analytics hook)
    │   ├── calendar.py         ← /api/calendar
    │   ├── email.py            ← /api/email
    │   ├── episodic.py         ← /api/memory/episodic
    │   ├── feeds.py            ← /api/feeds (Phase 15, 14 routes)
    │   ├── analytics.py        ← /api/analytics (Phase 17, 8 routes)
    │   └── plugins.py          ← /api/plugins (Phase 18, 9 routes)
    ├── agents/
    │   ├── coordinator.py      ← TaskCoordinator + _REGISTRY (Phase 10)
    │   ├── research_agent.py   ← ResearchAgent + ResearchAgentV2
    │   ├── task_decomposer.py
    │   ├── scheduler_agent.py
    │   ├── browser_agent.py
    │   ├── code_agent.py
    │   ├── cad_agent.py
    │   ├── vision_agent.py
    │   ├── calendar_agent.py
    │   ├── email_agent.py
    │   └── reminder_agent.py
    ├── analytics/
    │   ├── __init__.py
    │   ├── collector.py        ← Phase 17 SQLite analytics
    │   └── middleware.py       ← Phase 17 HTTP middleware
    ├── feeds/
    │   ├── __init__.py
    │   ├── weather.py          ← Phase 15
    │   ├── stocks.py           ← Phase 15
    │   ├── news.py             ← Phase 15
    │   └── crypto.py           ← Phase 15
    ├── iot/
    │   ├── mqtt_client.py      ← Phase 16 (extended)
    │   ├── home_assistant.py   ← Phase 16 (scenes/scripts/automations)
    │   ├── device_groups.py    ← Phase 16 NEW
    │   ├── nl_mapper.py
    │   └── ws_listener.py
    ├── memory/
    │   ├── chroma_db.py        ← 4 collections: conversations/documents/knowledge/episodes
    │   ├── embeddings.py
    │   ├── episodic_memory.py
    │   ├── ingestion.py
    │   ├── rag.py
    │   └── supabase_client.py
    ├── plugins/
    │   ├── __init__.py
    │   ├── plugin_manifest.py  ← Phase 18 Pydantic schema
    │   └── plugin_registry.py  ← Phase 18 dynamic loader
    ├── system/
    │   ├── control.py          ← OS control (psutil, winreg)
    │   ├── powershell.py
    │   ├── files.py
    │   ├── executor.py
    │   └── browser.py
    └── voice/
        ├── tts.py              ← Phase 14 (ElevenLabs→Kokoro→edge-tts→pyttsx3)
        ├── stt.py
        ├── vad.py
        └── wake_word.py
```

---

## Critical Workflow Rules

### 1. Unicode Truncation Bug (PERMANENT RULE)
Files containing `──` (U+2500 box-drawing chars) **must** be written via bash heredoc:
```bash
cat > /tmp/file.py << 'ENDOFFILE'
...code...
ENDOFFILE
cp /tmp/file.py /sessions/.../src/path/file.py
```
**Never use the Edit tool on files with box-drawing chars.** The edit succeeds silently but truncates the file, causing `SyntaxError: expected an indented block`.

### 2. AST Verification (after every write)
```bash
python3 -c "import ast; ast.parse(open('/tmp/file.py').read()); print('OK')"
# NOT: python3 -m py_compile  (permission error on /tmp/__pycache__)
```

### 3. JS Syntax Verification
```bash
node --check static/js/panels.js
```

### 4. Bash Path Mapping (Cowork — changes each session)
| Windows | Bash |
|---|---|
| `D:\Tron_X\` | Check with `ls /sessions/*/mnt/Tron_X/` |
| outputs | `/sessions/*/mnt/outputs/` |

**Always `ls /sessions/` first in a new session** to find the current mount path.

### 5. Router Prefix Discipline
Prefix set at `APIRouter(prefix="/api/x")`. Route decorators use relative paths only:
```python
router = APIRouter(prefix="/api/feeds")
@router.get("/weather/current")   # NOT "/api/feeds/weather/current"
```

### 6. Middleware Registration Order (main.py)
Starlette applies middleware in **reverse registration order** (last registered = outermost).
Current order — register auth LAST so it's the outermost gate:
```python
add_analytics_middleware(app)   # registered first = innermost
add_rate_limit_middleware(app)  # middle
add_auth_middleware(app)        # registered last = outermost ✓
```

### 7. Fire-and-Forget Analytics
```python
asyncio.create_task(get_collector().record_chat(...))
# + try/except Exception: pass  ← analytics must never crash the app
```

---

## Key API Endpoints Quick Reference

| Group | Method | Path | Notes |
|---|---|---|---|
| Health | GET | `/api/health` | Always unauthenticated |
| Chat | POST | `/api/chat` | `{message, session_id, intent, persona}` |
| Chat stream | POST | `/api/chat/stream` | SSE: meta→text→done |
| Voice stream | POST | `/api/voice/stream` | SSE: transcript→audio_chunk→done |
| Weather | GET | `/api/feeds/weather/current` | `?location=London` |
| Crypto | GET | `/api/feeds/crypto/price` | `?coin=bitcoin` |
| Stocks | GET | `/api/feeds/stocks/quote` | `?symbol=AAPL` |
| News | GET | `/api/feeds/news/headlines` | `?topic=tech&count=6` |
| IoT devices | GET | `/api/iot/devices` | HA entity list |
| IoT control | POST | `/api/iot/control/{id}` | `{service: "turn_on"}` |
| MQTT topics | GET | `/api/iot/mqtt/topics` | Active subscriptions |
| Analytics | GET | `/api/analytics/summary` | Request + event counts |
| Agents registry | GET | `/api/agents/coordinate/registry` | All registered agents |
| Agent run | POST | `/api/agents/coordinate/single` | `{agent, payload}` |
| Plugins | GET | `/api/plugins` | List all plugins |
| Plugin scan | POST | `/api/plugins/scan` | Reload from disk |
| System info | GET | `/api/system/info` | cpu_percent, ram_used_pct, disk_used_pct |
| Memory stats | GET | `/api/memory/stats` | ChromaDB collection counts |
| Sessions | GET | `/api/chat/sessions` | Active session IDs |

---

## Environment Variables (Phase 20 additions)

```env
# Auth (off by default)
AUTH_ENABLED=false
API_KEYS=key1,key2        # generate: python -c "import secrets; print(secrets.token_hex(32))"
AUTH_SKIP_PATHS=/health,/docs,/redoc,/static,/openapi.json

# Rate limiting (off by default)
RATE_LIMIT_ENABLED=false
RATE_LIMIT_RPM=60
RATE_LIMIT_SKIP_PATHS=/health,/static
```

---

## Known Issues & Fixes Applied

| Issue | Root Cause | Fix |
|---|---|---|
| wttr.in weather fails | aiohttp rejects `text/plain` Content-Type for JSON | `r.json(content_type=None)` in `src/feeds/weather.py` |
| System Monitor shows `—` | HUD called `/api/system/status` (doesn't exist) | Updated `panels.js` to call `/api/system/info` + mapped field names (`ram_used_pct`, `disk_used_pct`) |
| `GET /` returns 404 | No root route defined | Added `@app.get("/")` redirect to `/static/index.html` in `main.py` |
| Edit tool truncates files | U+2500 box chars in source comments | Permanent rule: always write via bash heredoc |

---

## Production Deployment

### Docker
```bash
cp .env.example .env && nano .env
docker compose up -d
docker compose logs -f tronx
```

### systemd (bare metal)
```bash
sudo useradd --create-home tronx
sudo mkdir /opt/tronx && sudo chown tronx:tronx /opt/tronx
# copy project to /opt/tronx, create venv, pip install -r requirements.txt
sudo cp deploy/tronx.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tronx
journalctl -u tronx -f
```

---

## What's Left (post-session ideas)

- **HUD chat response styling** — JARVIS reply text colour could be brighter (currently matches dim background on some screens)
- **Analytics panel** — counts not showing; check `/api/analytics/summary` field names (`total_requests` vs `requests`)
- **IoT panel** — "SCANNING…" indefinitely if HA not configured; add a graceful "NOT CONFIGURED" state
- **Weather on HUD** — Now fixed (content_type=None), but needs server restart to take effect
- **Voice in HUD** — No microphone/STT UI yet; could add a push-to-talk button
- **Plugin hot-reload UI** — HUD could show a "SCAN PLUGINS" button in the Agents panel

---

*Generated: 2026-06-08 · TRON-X Session 2 · Phases 14–20 complete*
