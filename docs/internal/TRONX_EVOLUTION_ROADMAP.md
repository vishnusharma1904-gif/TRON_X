# TRON-X Evolution Roadmap — Phases 21-36

Implementation plan for Evolution Blocks A/B/C (Phases 21-30) plus 6 additional
proposed phases (31-36). No code has been written yet — this is the plan,
grounded in the current codebase, for review before implementation begins.

**Verdict: yes, all 10 original phases are feasible.** Three of them (24, 26, 27,
30) depend on resources outside this sandbox — local Docker, microphone/speaker
hardware, network tunneling — so for those, the code/config will be built here
but final testing happens on your machine. Effort estimates: S = 1 session,
M = 2-3 sessions, L = 3-5 sessions, XL = needs splitting into sub-phases.

---

## Block A — Intelligence & Reasoning

### Phase 21 — Stateful Supervisor & Dynamic Plan Revision ✅ IMPLEMENTED

**Status:** Implemented and tested — new `src/agents/supervisor.py`
(`SupervisorAgent(max_revisions=3).run(goal, persona, session_id)`) drives
`plan_tasks()`'s plan one step at a time via `_execute_subtask()`, calling a
new `revise_plan()` (added to `task_decomposer.py`, with robust JSON-parsing
helpers `_parse_revision()`/`_extract_json_object()`) after each step.
`"done"` stops early, `"replace_remaining"` swaps the not-yet-executed steps
for a new plan, `"continue"` is a no-op; capped at `max_revisions` real
revisions. Every real revision is logged to
`memory/cache/plan_revisions.jsonl`. Wired into `/api/agents/run` via a new
opt-in `AgentTaskReq.supervised: bool = False` field — `false` (default)
preserves the existing `run_agent_pipeline()` behavior unchanged. 40/40
checks pass in `tests/test_phase21_supervisor.py`; existing suites (Phase
22/23/28/29/34) re-run with no regressions. **Reconciliation:** this codebase
has no `TaskDecomposer`/`TaskCoordinator` classes or `complex_task`
orchestrator intent as assumed below — integrated at the existing
`/api/agents/run` endpoint instead. Real end-to-end run with a live LLM
re-planning a multi-step goal still pending.

**Feasibility:** High. `src/agents/task_decomposer.py` (240 lines) already does
LLM-based plan generation + routing to `AGENT_TYPES`, and `coordinator.py`
(276 lines, `TaskCoordinator`) already executes/aggregates. The missing piece is
a feedback loop: after each sub-task result, re-evaluate the remaining plan.

**Approach:**
- Add `SupervisorAgent` (new file `src/agents/supervisor.py`) wrapping
  `TaskDecomposer` + `TaskCoordinator`.
- After each sub-task completes, feed `{result, success/failure, remaining_plan}`
  back to the planner LLM with a "revise plan" prompt — can insert/remove/
  reorder remaining steps, or mark the goal complete early.
- Cap revisions (e.g. max 3 re-plans) to avoid infinite loops; log every
  revision to `memory/cache/plan_revisions.jsonl` for debugging.
- Wire into orchestrator as an opt-in path for `intent == "complex_task"`.

**Files:** new `src/agents/supervisor.py`, edits to `task_decomposer.py`,
`coordinator.py`, `orchestrator.py`. **Effort: M.**

---

### Phase 22 — Local Intent Cache & Semantic Command Routing ✅ IMPLEMENTED
**Status:** Done. New `src/intelligence/intent_cache.py` (SQLite-backed,
brute-force cosine similarity, whitelist `{"chat","iot"}`, TTL eviction,
"clear command cache" command). Wired into `intent.py` (semantic-cache stage
before keyword classification) and `nl_mapper.py` (cache stage between
fast-path regex and LLM fallback, with an entity-existence recheck against
`_DEVICE_ALIASES`). New `INTENT_CACHE_*` settings in `config.py` /
`.env.example`. Verified via `tests/test_phase22_intent_cache.py`
(52/52 checks pass).

**Feasibility:** High. Embedding infra (`embed_one`, ChromaDB) already exists.

**Approach:**
- New ChromaDB collection `COL_INTENT_CACHE` (or lightweight SQLite table —
  SQLite is simpler for exact+semantic hybrid lookups and avoids polluting
  the vector store with high-churn entries).
- On each message: embed → query cache for nearest neighbor. If similarity
  ≥ 0.98 AND cached intent is a deterministic action (IoT command, "what time
  is it", etc.), skip `IntentClassifier` + LLM entirely and dispatch straight
  to `src/iot/nl_mapper.py` / command handlers.
- Cache populated automatically: every time `IntentClassifier` returns a
  high-confidence result for an actionable intent, store `(embedding, intent,
  resolved_action)`.
- Add TTL/eviction (e.g. 30 days) and a manual "forget cached commands" admin
  command for when device names/routines change.

**Files:** new `src/intelligence/intent_cache.py`, edits to `intent.py`,
`commands.py`, `orchestrator.py`. **Effort: M.**

**Caveat:** must be conservative — only cache *non-destructive, deterministic*
intents (lights, music, time/weather). Never cache intents that touch email,
WhatsApp sends, file deletion, or anything with parameters that vary
meaningfully (the embedding similarity could be high for "turn off the lights"
vs "turn off the lights in 30 minutes").

---

### Phase 23 — Context-Aware Dynamic Prompt Pruning ✅ IMPLEMENTED
**Status:** Done. `_score_turns()` + new `_trim_history()` in `orchestrator.py`,
gated by `PRUNING_STRATEGY` ("semantic" default | "fifo" rollback) in
`config.py` / `.env.example`. Verified via `tests/test_phase23_pruning.py`
(30/30 checks pass).

**Feasibility:** High. `orchestrator.py` already has `_build_messages()` /
`_trim_history()` and `MAX_CONTEXT_TOKENS = 8000`; SentenceTransformers is
already a dependency.

**Approach:**
- Replace FIFO popping in `_trim_history()` with a scoring pass:
  - Compute embedding similarity between each historical turn and the
    *current* user message → relevance score.
  - Always preserve: system prompt, last N=3 turns (recency anchor), any turn
    flagged as containing a "remember"/fact-store action, and any turn with
    RAG-injected context (high information density).
  - Drop lowest-scoring turns first until under `MAX_CONTEXT_TOKENS`.
- Make this pluggable: `PRUNING_STRATEGY = "semantic" | "fifo"` env var so it
  can be A/B'd or rolled back instantly.

**Files:** edits to `orchestrator.py` only (`_trim_history`, `_build_messages`).
**Effort: S-M.**

---

## Block B — Advanced Peripherals & Visual Context

### Phase 24 — Background Continuous Visual Episodic Memory
**Feasibility:** Medium-High, technically straightforward, but **this is the
one phase that needs a privacy decision before any code is written.**
Continuous desktop screenshotting + OCR creates a searchable log of everything
you look at — passwords typed in plaintext fields, private messages, financial
info, etc.

**Approach (if you proceed):**
- New cron job (apscheduler, alongside existing scheduler) using `mss`
  (already a dependency) to capture screenshots at a configurable interval
  (e.g. every 30-60s, only when screen is unlocked/active).
- OCR via Tesseract (lighter, already common) — EasyOCR is heavier (PyTorch)
  and likely overkill; recommend Tesseract first, EasyOCR as opt-in upgrade.
- New ChromaDB collection `screen_history` with metadata `{timestamp, app_name,
  window_title}`.
- **Mandatory safeguards**: an app-name blocklist (password managers, banking
  sites, browser private/incognito windows skipped), a global on/off toggle
  exposed in the HUD, configurable retention (auto-purge after N days), and the
  data stays 100% local (never sent to cloud LLMs as raw screenshots — only
  OCR'd text, and only when relevant to a query).

**Files:** new `src/vision/screen_recorder.py`, new collection in
`chroma_db.py`, scheduler registration, HUD toggle. **Effort: L.**

**Recommendation:** build with the toggle defaulting to OFF, ship it, and you
opt in once you've reviewed the blocklist/retention settings.

---

### Phase 25 — Local Speaker Biometrics & Voice-Keyed Authorization
**Feasibility:** High. Resemblyzer is pure-Python/PyTorch, pip-installable, no
GPU required for single-speaker verification at this scale.

**Approach:**
- Enrollment flow: record 3-5 short voice samples → compute speaker embedding
  (Resemblyzer `VoiceEncoder`) → store in `memory/cache/voice_profile.npy`.
- On wake-word trigger (`src/voice/wake_word.py`), after VAD captures the
  utterance, compute embedding and cosine-compare to enrolled profile.
- Add `voice_verified: bool` to the request context passed into
  `orchestrator.chat()`.
- Gate a small, explicit list of "destructive" commands (file deletion, system
  shutdown, sending money/messages, executing code, modifying automations)
  behind `voice_verified == True`. Everything else works regardless (so a
  guest/different voice can still chat normally).
- Threshold tuning + a fallback PIN/passphrase for when voice doesn't match
  (cold, sick, mic issues).

**Files:** new `src/voice/biometrics.py`, edits to `wake_word.py`,
`commands.py` (gate check), `executor.py`. **Effort: M.**

---

### Phase 26 — Real-Time Audio Streaming & Duplex Interruption
**Feasibility:** Medium. This is the largest lift in Block B — it touches
backend (WebSocket audio pipeline), TTS engine (`src/voice/tts.py`, currently
edge-tts/Kokoro — neither natively supports mid-utterance cancellation, so
playback must be chunked), and the Three.js HUD frontend (`static/`).

**Approach:**
- New WebSocket endpoint (`/ws/voice`) replacing/augmenting the current
  request-response voice flow.
- TTS output generated in sentence-level chunks, streamed to client as they're
  ready; client plays via Web Audio API queue.
- Continuous VAD on the client mic stream while TTS is playing — on detected
  speech onset, send an `interrupt` message over the WS, server stops
  generating further chunks and clears the client's playback queue.
- Barge-in handling: partial assistant response (what was actually spoken) is
  what gets stored in conversation history, not the full generated text.

**Files:** new `src/api/voice_ws.py`, edits to `tts.py`, `wake_word.py`,
HUD audio player module. **Effort: L-XL** — recommend splitting into 26a
(streaming TTS playback, no interruption) and 26b (VAD-based interruption).

---

## Block C — Hardened Infrastructure & Integrations

### Phase 27 — Ephemeral Docker Container Code Sandbox
**Feasibility:** High for the code; **requires Docker Desktop installed and
running on your Windows machine** (with WSL2 backend) — I can't test container
execution from this sandbox, only write/validate the integration code.

**Approach:**
- Replace `_ast_scan`-based gating in `src/system/executor.py` with: spin up a
  short-lived container (`python:3.12-slim` / `node:20-slim`) per execution
  request via the `docker` Python SDK.
- Resource caps: `mem_limit`, `nano_cpus`, `network_mode="none"` (default —
  opt-in network only for explicitly whitelisted tasks), `pids_limit`,
  read-only root filesystem with a tmpfs `/tmp`.
- Mount only a per-execution scratch dir; auto-remove container after
  execution or timeout (`_kill_proc` logic carries over conceptually).
- Keep the existing AST scan as a **first-pass fast filter** (reject obviously
  malicious code before even spinning up a container) — defense in depth.
- Fallback: if Docker isn't available at runtime, fall back to the current
  AST-sandboxed subprocess approach with a logged warning, so the feature
  degrades gracefully rather than breaking code execution entirely.

**Files:** rewrite core of `src/system/executor.py`, new
`src/system/docker_sandbox.py`, new `requirements.txt` entry (`docker`).
**Effort: L.**

---

### Phase 28 — Proactive Cron Analytics & Diagnostic Self-Healing ✅ IMPLEMENTED
**Status:** Done. New `src/system/self_healing.py` runs a periodic
`run_health_check()` (RAM/disk via `psutil`, circuit-breaker state via
`HealthTracker.get_status_summary()`), with anti-thrashing state tracking and
a `diagnostics.jsonl` ring-buffer (`_log_diagnostic` / `get_diagnostics_log`).
`router.py` gained `SmartRouter.bias_fallback_chain()` / `bias_status()` /
`_check_bias_revert()` so the self-healer can temporarily reorder fallback
chains toward a healthy model, with auto-revert on TTL expiry or once tripped
models recover. `chroma_db.py` gained
`ChromaManager.delete_old_conversations()` for disk-pressure pruning (only
ever touches the `conversations` collection). New `src/api/system_health.py`
exposes `/api/system/health/status|diagnostics|check`, registered in
`main.py` along with a scheduled `self_healing_check` job gated by
`SELF_HEALING_ENABLED` (default on). New "System Health" HUD card in
`panels.js`. New settings in `config.py` / `.env.example`:
`SELF_HEALING_ENABLED`, `SELF_HEALING_INTERVAL_SEC`, `RAM_THRESHOLD_PCT`,
`DISK_THRESHOLD_PCT`, `CIRCUIT_TRIP_REORDER_THRESHOLD`. Verified via
`tests/test_phase28_self_healing.py` (74/74 checks pass).

**Feasibility:** High — strong existing foundation. `src/analytics/collector.py`
(493 lines) and `middleware.py` already collect metrics, and `psutil` is
already used in `src/system/control.py`.

**Approach:**
- New scheduled job (apscheduler) running every N minutes:
  - `psutil` checks: RAM %, CPU %, disk %, process count.
  - Reads `HealthTracker` circuit-breaker state from `router.py`.
- Self-healing actions:
  - RAM > threshold → clear in-memory log/analytics caches, trigger
    `gc.collect()`, optionally drop oldest ChromaDB conversation entries
    beyond a configurable cap.
  - Repeated circuit-breaker trips on primary models → temporarily reorder
    `SmartRouter`'s fallback chain to prefer healthier/cheaper models, with
    auto-revert once the primary recovers (health check ping).
  - Disk > threshold → purge old logs, old screen_history (Phase 24) entries,
    old session caches.
- All actions logged to a `diagnostics.jsonl` and surfaced via a new "System
  Health" HUD card.

**Files:** new `src/system/self_healing.py`, edits to `router.py` (expose
health-state query API), `analytics/collector.py`, scheduler registration,
new HUD card. **Effort: M.**

---

### Phase 29 — Local Embedding Offloading & Ollama Mesh Fallback ✅ IMPLEMENTED

**Status:** Implemented and tested — `_check_ollama_health()` (cached,
2s timeout), `_OLLAMA_INTENT_MAP` + `_OLLAMA_FALLBACK_PRIORITY` fallback chain
in `router.py` replacing the final `AllProvidersExhaustedError` raise (with
fallthrough on per-model errors and a prepended local-fallback disclaimer),
`ollama_fallback_enabled`/`ollama_health_check_interval_sec`/
`embedding_backend` settings in `config.py`, `_embed_ollama()` +
backend-toggle in `embeddings.py` (default SentenceTransformers unchanged),
and `scripts/check_ollama.py`. 56/56 checks pass in
`tests/test_phase29_ollama_fallback.py`; full existing suite (Phase 22/23/28/34)
re-run with no regressions. Real end-to-end test against a live local Ollama
instance still pending.

**Feasibility:** High — head start already exists. `config/models.json` already
lists `ollama` as a provider with 7 models, and `src/core/config.py` already
has `ollama_base_url`. **Requires Ollama installed and running locally** (or on
a LAN box) to actually use it — code/config will be ready either way.

**Approach:**
- Add an Ollama health-check (ping `ollama_base_url/api/tags`) on startup and
  periodically; if reachable, mark local models as available in
  `HealthTracker`.
- Extend `SmartRouter`'s fallback chain logic: when *all* external providers
  in a chain are circuit-broken or rate-limited, fall back to a local Ollama
  model (`llama3.2` / `qwen2.5` / `mistral` per `models.json`) rather than
  raising `AllProvidersExhaustedError`.
- Embedding offload: `src/memory/embeddings.py` already uses local
  SentenceTransformers (`all-MiniLM-L6-v2`) — confirm this stays fully local
  (it does); add a config flag to optionally swap to an Ollama embedding model
  if the user wants larger embeddings without extra pip deps.
- Add a one-time setup helper script that checks for Ollama, and if missing,
  prints the install command + suggests `ollama pull llama3.2`.

**Files:** edits to `router.py`, `config.py`, `embeddings.py`, new
`scripts/check_ollama.py`. **Effort: S-M.**

---

### Phase 30 — Distributed Memory Mesh & Secured Mobile API Tunneling
**Feasibility:** Medium. The PWA + JWT auth backend can be fully built here.
**Cloudflare Tunnel / Tailscale setup is network configuration on your machine
and accounts** — I can write the config files (`cloudflared` config, Tailscale
ACLs) and step-by-step instructions, but the actual tunnel registration
requires your Cloudflare/Tailscale account credentials, which I won't handle.

**Approach:**
- Add JWT-based auth middleware to `src/main.py` (FastAPI) — login endpoint
  issuing short-lived tokens, required for all `/api/*` routes when accessed
  remotely.
- Build a minimal mobile-first PWA (`static/mobile/`) — chat interface +
  quick-action buttons (IoT, reminders, memory search), syncing via the
  existing WebSocket/SSE endpoints. Add `manifest.json` + service worker for
  installability/offline shell.
- "Distributed memory mesh": if you run TRON-X on more than one machine
  (desktop + a home server), sync the `knowledge`/`episodes` ChromaDB
  collections between them — simplest approach is a periodic export/import
  job (JSON dumps of collections) over the existing tunnel, rather than a true
  distributed DB (avoids heavy infra like a Chroma server cluster).
- Provide `cloudflared` / Tailscale config templates + a written setup guide
  as the deliverable for the tunneling piece.

**Files:** new `src/api/auth.py`, new `static/mobile/`, new
`scripts/sync_memory_mesh.py`, config templates + setup doc. **Effort: L**,
mostly front-loaded on the PWA.

---

## Additional Proposed Phases (31-36)

### Phase 31 — Adaptive Persona & Preference Learning
Track implicit signals (corrections, "no I meant...", verbosity preferences,
which suggestions get accepted) and periodically update a per-user "style
profile" stored in `COL_KNOWLEDGE`, injected into the system prompt via
`persona.py`. Lighter-weight alternative to fine-tuning — pure prompt
engineering + memory. **Effort: S-M.**

### Phase 32 — Browser Macro Recorder & Replay
Building on `src/agents/browser_agent.py` (Playwright): record a sequence of
browser actions once ("book my usual train"), save as a parameterized macro
(JSON), replay on command or schedule with substituted parameters (date,
destination). Natural pairing with the scheduler agent. **Effort: M.**

### Phase 33 — Encrypted Memory Backup & Disaster Recovery ✅ IMPLEMENTED
**Status:** Done. New `src/system/backup.py` (Fernet/PBKDF2-HMAC-SHA256,
480k iterations, random salt per archive) + `scripts/restore_backup.py`
(decrypt, list, confirm, `memory_pre_restore/` safety copy, extract).
Snapshots `memory/chroma`, `memory/cache/sessions.json`, plus
Phase 25/31 cache files if present, into `tronx_backup_<ts>.tar.enc` with
`_enforce_retention()`. Opt-in cron job `"encrypted_memory_backup"` wired
into `src/main.py`'s lifespan via new config fields `backup_enabled` (default
`False`), `backup_dir`, `backup_retention_count`, `backup_passphrase`,
`backup_cron`. Deviates from the original "default-on, fail loudly" plan in
favor of opt-in + non-fatal startup warning (see HANDOFF.md Phase 33 for
rationale). Verified via `tests/test_phase33_backup.py` (47/47 checks pass).

Scheduled (apscheduler) export of ChromaDB collections + `sessions.json` +
WhatsApp bridge auth state to an encrypted archive (age/gpg with a
user-supplied key), written to a local backup folder or a destination you
configure. Restore script included. Protects against disk failure / corrupted
ChromaDB. **Effort: S.**

### Phase 34 — Unified Cost & Usage Dashboard ✅ IMPLEMENTED
**Status:** Done. New `config/pricing.json` (provider defaults + model
overrides + `:free` markers) drives `collector._price_for_model()` /
`get_cost_summary()`, returning total/by-provider/by-model/by-intent costs,
token totals, `unpriced_models`, `pricing_last_updated`,
`cache_hit_rate: None` (Phase 22 placeholder), and `circuit_breaker_events`
(Phase 28 integration). New `GET /api/analytics/dashboard` endpoint and a
`renderCostDashboard()` HUD card wired to a new `cost` intent in `panels.js`.
Verified via `tests/test_phase34_cost_dashboard.py` (106/106 checks pass).

Surface `analytics/collector.py` data (already tracking per-call metrics) as a
HUD card: tokens/cost per provider/model over time, cache-hit rate (ties into
Phase 22), circuit-breaker history (ties into Phase 28). Mostly frontend +
aggregation queries. **Effort: S-M.**

### Phase 35 — Automation Rules Engine ("if this then that")
Generalizes `src/iot/nl_mapper.py` + `reminder_agent.py` + `scheduler_agent.py`
into a rules engine: trigger (time, IoT state change, calendar event, voice
phrase) → condition → action (any existing agent capability). Stored as
JSON rules, editable via natural language ("when I say goodnight, turn off all
lights and arm the security mode"). **Effort: M-L.**

### Phase 36 — Self-Tuning Router via A/B Feedback
`router.py` already has an A/B testing framework (Phase 3) — close the loop by
correlating A/B variant choice with downstream signals (user
edits/regenerations, response latency, eventual `forget`/correction commands)
to automatically shift traffic toward better-performing models per intent
category, with a manual override/audit log. **Effort: M.**

---

## Recommended Sequencing

| Batch | Phases | Why this order |
|---|---|---|
| 1 | 23, 28, 34 | Pure backend/quality wins, low risk, build on existing code (`_trim_history`, `analytics/collector.py`). Sets up better foundations for everything else. |
| 2 | 22, 29 | Performance + resilience. 29 has a head start (Ollama already in catalog). |
| 3 | 21, 33 | Supervisor loop (builds on 22/23 for cleaner context); backup is cheap insurance to add once memory mode (done) is in heavier use. |
| 4 | 27 | Security-critical sandbox upgrade — do this before exposing any remote/mobile access (Phase 30). |
| 5 | 25, 35 | Voice biometrics + automation rules — independent, can interleave. |
| 6 | 24 | **Pending your privacy review** of the screen-recording design (default OFF). |
| 7 | 26 | Largest lift (26a streaming, then 26b interruption) — best done once core is stable. |
| 8 | 30, 31, 32, 36 | Remote access + polish features last, after security (27) and core stability are solid. |

---

## Open Decisions Needed From You

1. **Phase 24**: comfortable with the privacy safeguards described (opt-in,
   blocklist, local-only, retention limit)? Or skip/defer this phase?
2. **Phase 27**: do you have Docker Desktop (with WSL2) installed/runnable on
   the TRON-X machine? If not, I can still write the integration but it won't
   be testable until you install it.
3. **Phase 29**: is Ollama already installed locally? If not I'll include the
   setup script but the fallback will stay dormant until it's running.
4. **Phase 30**: do you have a Cloudflare account / Tailscale set up already,
   or should the deliverable just be the PWA + auth backend + setup docs for
   later?
5. **Sequencing**: happy with the batch order above, or is there a phase you
   want prioritized (e.g. if remote mobile access is the main motivator, we'd
   pull Phase 30 forward after Phase 27)?

Once you confirm, I'll start with Batch 1.
