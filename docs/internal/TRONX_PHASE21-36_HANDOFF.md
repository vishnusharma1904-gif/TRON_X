# TRON-X Phase 21-36 Implementation Handoff

This is a self-contained spec for implementing Phases 21-36 (Evolution Blocks
A/B/C + 6 additional phases). Written for a fresh session/model with no prior
context. Companion to `TRONX_EVOLUTION_ROADMAP.md` (feasibility/sequencing) —
this doc adds concrete interfaces, file-level changes, and verification steps.

No code has been written for these phases yet. "Memory mode" (remember/forget
facts via `COL_KNOWLEDGE`) was completed in the prior session and is the most
recent working example of the conventions below — look at
`src/intelligence/memory_commands.py`, `src/memory/chroma_db.py`
(`remember_fact`/`find_facts`/`forget_fact`/`list_facts`), `src/memory/rag.py`
(`retrieve_knowledge`), and the `3a`/`4a` blocks in
`src/intelligence/orchestrator.py` as a template for "new capability hooked
into chat() and chat_stream()".

---

## 0. Quick Start & Permanent Workflow Rules

```bash
cd D:\Tron_X
uvicorn src.main:app --reload
# http://127.0.0.1:8000
```

1. **Bash sandbox path mapping**: `D:\Tron_X` ↔ `/sessions/*/mnt/Tron_X/`.
   Use Read/Edit/Write tools with Windows paths; use bash with the mapped
   Linux path.
2. **Stale mount risk**: occasionally a file edited via the Edit tool doesn't
   sync to the bash mount immediately for `py_compile`. If `py_compile` fails
   with an error pointing at code that doesn't match what you just wrote,
   write a verification copy to `outputs/verify/<file>.py` (read via Read
   tool, write via Write tool) and `py_compile` that instead — don't assume
   your edit is broken.
3. **NEVER use the Edit tool on files containing box-drawing characters**
   (`─`, U+2500 — common in Python module docstrings/banners and all of
   `static/css`, `static/js`, `static/index.html`). Edit tool corrupts these.
   Use `cat > /tmp/file << 'EOF' ... EOF` then `cp` into place, or rewrite via
   the Write tool (which is safe) instead of Edit.
4. **Verify after every write**:
   - Python: `python3 -m py_compile <file>`
   - JS: `node --check <file>`
   - Run the existing test harness pattern (see memory_commands.py session —
     standalone script with hand-written test cases) for any new
     parser/regex/scoring logic before wiring it in.
5. **`.env` / secrets**: never commit, already gitignored. New config values
   go through `src/core/config.py` (`pydantic-settings`, `Field(default=...)`)
   and `.env.example`.
6. **Async conventions**: ChromaDB writes go through `ChromaManager._lock`
   (`asyncio.Lock`). New collections follow the `COL_*` constant pattern in
   `chroma_db.py` and are registered in `_init_collections()`.
7. **Persona-aware replies**: any user-facing string should branch on
   `persona` ("jarvis" vs "friday") per existing patterns in
   `memory_commands.py` (`handle_remember`/`handle_forget`).
8. **`_is_internal` sessions**: session IDs starting with `__` are internal
   (e.g. background jobs calling `orchestrator.chat()`). New always-on hooks
   in `chat()`/`chat_stream()` should be gated `if not _is_internal:` like the
   memory-mode `3a`/`4a` blocks, unless the phase specifically needs to run
   for internal sessions too (e.g. Phase 28's self-healing).

---

## 1. Architecture Cheat Sheet

```
src/
  main.py                 # FastAPI app, router registration (app.include_router)
  core/
    config.py             # pydantic-settings; ollama_base_url already present
    logger.py             # `log` — rich-based logger
    exceptions.py          # TronXError, AllProvidersExhaustedError, ProviderError, RateLimitError
  intelligence/
    orchestrator.py        # Orchestrator (singleton get_orchestrator()), chat()/chat_stream()
                            #   _build_messages/_trim_history (line ~250-270), MAX_CONTEXT_TOKENS=8000
                            #   826 lines total
    intent.py               # IntentClassifier, _keyword_classify, _llm_classify (257 lines)
    commands.py             # try_handle_command() dispatcher (~590 lines)
    memory_commands.py       # remember/forget command parsing+handlers (NEW, 196 lines)
    router.py               # SmartRouter, HealthTracker (circuit breaker), 104 models/14 providers
    persona.py               # PersonaEngine.build_system_prompt()
    prompts.py               # RAG_CONTEXT_TEMPLATE, INTENT_CLASSIFICATION_PROMPT, etc.
  memory/
    chroma_db.py             # ChromaManager singleton get_chroma()
                              #   COL_CONVERSATIONS, COL_DOCUMENTS, COL_KNOWLEDGE, COL_EPISODES
                              #   remember_fact/find_facts/list_facts/forget_fact (NEW)
    rag.py                    # RAGPipeline singleton get_rag(), retrieve()/retrieve_knowledge() (NEW)
    embeddings.py             # embed()/embed_one() — SentenceTransformers all-MiniLM-L6-v2, local
    episodic_memory.py        # remember()/recall()/forget_* for session episodes
  agents/
    task_decomposer.py        # TaskDecomposer — LLM plan generation (240 lines)
    coordinator.py             # TaskCoordinator — executes/aggregates plan (276 lines)
    browser_agent.py, cad_agent.py, code_agent.py, research_agent.py, etc.
  voice/
    wake_word.py, tts.py       # edge-tts / Kokoro ONNX local TTS
  system/
    executor.py                 # AST-sandboxed code exec (404 lines) — Phase 27 target
    control.py                   # psutil-based system stats
  analytics/
    collector.py                 # 493 lines — metrics collection, Phase 28/34 foundation
    middleware.py
  iot/
    nl_mapper.py                  # NL → Home Assistant command mapping — Phase 22 target
  api/
    chat.py, voice.py, analytics.py, iot.py, agents.py, ... (one router per domain,
    APIRouter(prefix="/api/...", tags=[...]), registered in main.py)
static/
  index.html, css/hud.css, js/{app,chat,hud,panels,scene,voice}.js   # Three.js HUD
config/
  models.json                    # provider_configs (incl. "ollama"), per-intent fallback chains
memory/
  cache/sessions.json             # session-persisted chat history
  chroma/                          # ChromaDB persistent store
```

---

## 2. Phase Specs

Each spec: **Goal**, **New/Changed Files**, **Interfaces**, **Integration
Points**, **Edge Cases**, **Verification**.

### Phase 21 — Stateful Supervisor & Dynamic Plan Revision ✅ IMPLEMENTED

**Status:** Implemented and tested (40/40 checks pass in
`tests/test_phase21_supervisor.py`; existing suites — Phase 22 (52/52 +1
skip), 23 (30/30), 28 (74/74 +1 skip), 29 (56/56), 34 (106/106) — re-run with
no regressions).

- New `src/agents/supervisor.py`: `SupervisorAgent(max_revisions=3).run(goal,
  persona, session_id)` runs `plan_tasks()`'s plan one step at a time via the
  existing `_execute_subtask()`, calling `revise_plan()` after each step
  (skipped once the plan is exhausted). `"done"` stops early;
  `"replace_remaining"` swaps all not-yet-executed steps for `new_plan`
  (kept steps + new tail); `"continue"` is a no-op. Once `max_revisions`
  real revisions (`done`/`replace_remaining`) have occurred, further
  `revise_plan()` calls are skipped and the current plan runs to completion
  (`capped: true`, with a note appended to the final reply).
  `revise_plan()` failures/timeouts (30s) degrade to `"continue"` and never
  abort the run.
- `src/agents/task_decomposer.py`: added `revise_plan()` (LLM call via
  `orchestrator.chat()` with a strict-JSON `_REVISE_PROMPT`) and
  `_parse_revision()`/`_extract_json_object()` for robust JSON extraction —
  handles markdown code fences, trailing commas, JSON embedded in prose, and
  falls back to `{"action": "continue"}` for anything unparseable or with an
  invalid `action` value. `run_agent_pipeline()` (the existing linear
  pipeline) is unchanged — no regression risk for current callers.
- Every real revision (`done`/`replace_remaining`) is appended to
  `memory/cache/plan_revisions.jsonl` (`timestamp`, `session_id`, `goal`,
  `revision_n`, `old_plan`, `new_plan`, `reason`); `"continue"` is not logged.
- **Reconciliation**: this codebase has no `TaskDecomposer`/`TaskCoordinator`
  classes or `complex_task` orchestrator intent as the spec assumed --
  `task_decomposer.py` exposes module-level `plan_tasks()` /
  `run_agent_pipeline()`, and `coordinator.py`'s `TaskCoordinator` is the
  Phase 10 registry-dispatcher (different purpose). Integrated instead at
  the existing `/api/agents/run` endpoint (`src/api/agents.py`): new opt-in
  `AgentTaskReq.supervised: bool = False` field routes to
  `SupervisorAgent().run()` instead of `run_agent_pipeline()` when `true` --
  no-op by default, per the verification protocol's "new flags must preserve
  current behavior" rule.
- Real end-to-end run (live LLM re-planning a multi-step goal) still pending;
  current verification mocks `plan_tasks`/`_execute_subtask`/`_synthesise`/
  `revise_plan`.

**Goal:** `TaskDecomposer`/`TaskCoordinator` currently generate a plan once and
execute it linearly. Add a feedback loop that re-evaluates the plan after each
sub-task using its actual result.

**New/Changed Files:**
- New `src/agents/supervisor.py`
- Edit `src/agents/task_decomposer.py` (expose a `revise_plan()` method
  alongside the existing planner)
- Edit `src/agents/coordinator.py` (loop through `SupervisorAgent` instead of
  static plan iteration, when supervisor mode is enabled)
- Edit `src/intelligence/orchestrator.py` (route `intent == "complex_task"` —
  or whatever multi-step intent already exists — through `SupervisorAgent`)

**Interfaces:**
```python
# src/agents/supervisor.py
class SupervisorAgent:
    def __init__(self, decomposer: TaskDecomposer, coordinator: TaskCoordinator,
                 max_revisions: int = 3): ...

    async def run(self, goal: str, session_id: str = "") -> dict:
        """
        1. plan = await decomposer.plan(goal)
        2. for each step in plan:
             result = await coordinator.execute_step(step)
             revised = await decomposer.revise_plan(goal, plan, completed=[...],
                                                       last_result=result)
             if revised.action == "done": break
             if revised.action == "replace_remaining": plan = revised.new_plan
             if revisions_used >= max_revisions: fall back to original plan tail
        3. return {"plan": plan, "results": [...], "revisions": n, "final": synthesis}
        """
```
- `revise_plan()` prompt must return strict JSON:
  `{"action": "continue"|"done"|"replace_remaining", "new_plan": [...]}` —
  reuse the JSON-parsing/retry pattern already in `task_decomposer.py`'s
  planner (look for how it currently parses LLM JSON output and handles
  malformed responses).

**Integration Points:**
- Log every revision to `memory/cache/plan_revisions.jsonl` (one JSON object
  per line: `{timestamp, session_id, goal, revision_n, old_plan, new_plan, reason}`).
- `Orchestrator.chat()`/`chat_stream()`: when intent indicates a multi-step
  goal, call `SupervisorAgent.run()` instead of the current direct
  decomposer→coordinator call (find that call site by searching for where
  `TaskDecomposer`/`TaskCoordinator` are currently invoked from orchestrator).

**Edge Cases:**
- Cap `max_revisions` (default 3) — if exceeded, execute the remaining
  original plan as-is and note in the response that re-planning was capped.
- If `revise_plan()` LLM call fails/times out, treat as `action: "continue"`
  (don't block on a re-plan failure).
- A sub-task failure should be passed into `revise_plan()` as
  `last_result.success = False` so the planner can route around it (e.g.
  retry with a different agent, or skip).

**Verification:** unit-test `revise_plan()` JSON parsing with malformed/edge
JSON inputs (markdown code fences, trailing commas, plain text). Integration
test: a 3-step goal where step 2 deliberately fails → confirm plan is revised
and execution doesn't crash.

---

### Phase 22 — Local Intent Cache & Semantic Command Routing ✅ IMPLEMENTED

**Status:** Implemented and verified (`tests/test_phase22_intent_cache.py`,
52/52 checks pass — real-embedding paraphrase-similarity check auto-skips if
SentenceTransformers unavailable; all threshold/whitelist/TTL/entity-recheck/
clear-command/dispatcher-integration checks pass unconditionally via an
injectable stub embedder). Two deliberate spec reconciliations, documented
in-code in `src/intelligence/intent_cache.py`:
  - `SAFE_CACHEABLE_INTENTS = {"chat", "iot"}` — this codebase's actual
    top-level intent taxonomy (`src/intelligence/intent.py::_INTENT_PATTERNS`);
    the spec's example names (`iot_light`, `time_query`, ...) aren't distinct
    intents here.
  - `MIN_CONFIDENCE_TO_STORE = 0.75` (not the spec's literal `0.9`) — the
    keyword classifier's "single confident match" ceiling for everyday IoT
    phrasings ("turn on the lights" = 0.85); the whitelist remains the
    primary safety boundary, per the spec's own framing.
Also added a fail-safe: if the sqlite store can't be opened (some FUSE/
network-mounted filesystems reject SQLite's locking model even for
`CREATE TABLE`), `IntentCache.enabled` becomes `False` and the cache
degrades to a no-op instead of crashing app startup.

**New/Changed Files (as built):**
- New `src/intelligence/intent_cache.py` (`IntentCache`, `CachedIntent`,
  `SAFE_CACHEABLE_INTENTS`, `MIN_CONFIDENCE_TO_STORE`, `get_intent_cache()`,
  `parse_clear_cache_command`/`handle_clear_cache`)
- Edit `src/intelligence/intent.py` (`IntentClassifier.classify()` — semantic
  cache lookup before keyword stage; stores fresh whitelisted classifications,
  resolving+storing the IoT device action via `nl_mapper.parse_command` too)
- Edit `src/iot/nl_mapper.py` (`nl_to_ha_command()` — cache lookup between the
  fast-path regex and the LLM fallback; stores successful LLM resolutions)
- Edit `src/intelligence/commands.py` (new "1c" dispatcher branch — "clear
  command cache" / "reset routines")
- Edit `src/core/config.py` + `.env.example` (`INTENT_CACHE_ENABLED=true`,
  `INTENT_CACHE_SIM_THRESHOLD=0.98`, `INTENT_CACHE_TTL_DAYS=30`)
- Edit `src/main.py` (startup TTL eviction + daily cron job, gated on
  `IntentCache.enabled`)

**Goal:** Skip `IntentClassifier` + LLM round-trip for high-confidence repeat
commands (lights on/off, "what time is it", music controls) via embedding
similarity ≥ 0.98 against a cache of prior classifications.

**New/Changed Files:**
- New `src/intelligence/intent_cache.py`
- Edit `src/intelligence/intent.py` (`IntentClassifier.classify()` — check
  cache first)
- Edit `src/intelligence/commands.py` (dispatch cached actionable intents
  directly to `src/iot/nl_mapper.py` or relevant handler, bypassing LLM)
- Edit `src/core/config.py` (add `INTENT_CACHE_SIM_THRESHOLD=0.98`,
  `INTENT_CACHE_TTL_DAYS=30`, `INTENT_CACHE_ENABLED=true`)

**Interfaces:**
```python
# src/intelligence/intent_cache.py
SAFE_CACHEABLE_INTENTS = {"iot_light", "iot_music", "time_query", "weather_query", ...}
# Whitelist only — anything not in this set is NEVER cached.

class IntentCache:
    def __init__(self, db_path: str = "memory/cache/intent_cache.sqlite"): ...

    async def lookup(self, message: str) -> Optional[CachedIntent]:
        """Embed message, find nearest neighbor via cosine sim over stored
        embeddings (load all into memory at startup — cache is small;
        re-index periodically). Return None if best sim < threshold or
        cached entry is expired (TTL) or intent not in SAFE_CACHEABLE_INTENTS."""

    async def store(self, message: str, intent: str, resolved_action: dict) -> None:
        """Only called when IntentClassifier returns confidence >= 0.9 AND
        intent in SAFE_CACHEABLE_INTENTS."""

    async def evict_expired(self) -> int: ...
    async def clear(self) -> int:
        """Manual 'forget cached commands' — exposed as a command, e.g.
        'clear command cache' / 'reset routines'."""

@dataclass
class CachedIntent:
    intent: str
    resolved_action: dict
    similarity: float
```

Use SQLite (not ChromaDB — high churn, simple key/value+vector). Store
embeddings as serialized numpy arrays (`BLOB`) or JSON float lists; for <1000
entries, brute-force cosine similarity in Python is fine (no need for an ANN
index).

**Integration Points:**
- `IntentClassifier.classify(message)`: first call
  `intent_cache.lookup(message)`. If hit, return `(intent, 1.0, cached=True)`
  and let `commands.py`/orchestrator skip straight to dispatch.
- After a *fresh* LLM classification with confidence ≥ 0.9 and intent in
  `SAFE_CACHEABLE_INTENTS`, call `intent_cache.store(...)`.
- Add a "clear command cache" command via `commands.py` (same dispatcher
  pattern as `parse_remember_command`).

**Edge Cases:**
- **Whitelist is the safety mechanism** — never cache: email/WhatsApp sends,
  file operations, code execution, calendar writes, anything with
  free-text/parametrized arguments where similarity could mask a different
  target (e.g. "turn off the lights" vs "turn off the lights in the office").
  For IoT, only cache if the resolved action's *device/entity ID* is also
  embedded as part of the cache key check — i.e. don't just match on
  utterance similarity, confirm the resolved entity still exists in
  `nl_mapper`'s current device list before dispatching from cache.
- TTL eviction: run on startup + daily via existing apscheduler.
- If `INTENT_CACHE_ENABLED=false`, `lookup()` always returns `None` (instant
  no-op fallback to current behavior).

**Verification:** seed cache with 10 known IoT phrasings, confirm paraphrases
("turn the lights off" vs "switch off the lights") hit cache at >0.98 sim;
confirm a destructive-sounding phrase ("delete my files") never gets cached
regardless of similarity (whitelist check, not just threshold).

---

### Phase 23 — Context-Aware Dynamic Prompt Pruning ✅ IMPLEMENTED

**Status:** Implemented and verified (`tests/test_phase23_pruning.py`, 30/30
checks pass — embedding-dependent topic-relevance check auto-skips if
SentenceTransformers unavailable, all structural/marker/FIFO-rollback checks
pass unconditionally). New config flag `PRUNING_STRATEGY` (`semantic` default
| `fifo`) in `src/core/config.py` + documented in `.env.example`.

**Goal:** Replace FIFO `_trim_history()` (orchestrator.py:250-256, currently
`messages.pop(1)` twice per iteration) with relevance-based pruning.

**Changed Files:**
- `src/intelligence/orchestrator.py` only (`_trim_history`, `_build_messages`,
  plus a new helper `_score_turns`)
- `src/core/config.py`: add `PRUNING_STRATEGY: str = "semantic"` (values:
  `"semantic" | "fifo"`)

**Interfaces:**
```python
# orchestrator.py
RECENCY_ANCHOR = 3  # always keep last N turns regardless of score

def _score_turns(self, messages: list[dict], current_query: str) -> list[float]:
    """
    messages[0] = system prompt (never scored/dropped)
    messages[1:] = alternating user/assistant turns (paired)
    Returns a relevance score per PAIR (user+assistant treated as one unit).
    Score = embedding similarity(pair_text, current_query), with bonuses:
      +0.5 if pair contains a 'remembered fact' marker (RAG/[Remembered fact])
      +0.3 if pair was a "remember"/"forget" command (check for ack strings
           from memory_commands.py, or tag turns with metadata when stored)
    """

def _trim_history(self, messages: list[dict], current_query: str = "") -> list[dict]:
    if PRUNING_STRATEGY == "fifo" or not current_query:
        # existing behavior, unchanged (fallback / backward compat)
        ...
    else:
        # 1. Always keep messages[0] (system) and last RECENCY_ANCHOR pairs
        # 2. Score remaining pairs via _score_turns
        # 3. Drop lowest-scoring pairs first until under MAX_CONTEXT_TOKENS
        # 4. Re-assemble in original chronological order (don't shuffle)
```

**Integration Points:**
- `_build_messages()` already has `user_content` available — pass it through
  to `_trim_history()` as `current_query` (it currently doesn't receive it).
- Embeddings: reuse `src.memory.embeddings.embed_one()` — already local/fast
  (all-MiniLM-L6-v2), no new dependency.
- To avoid re-embedding the full history on every turn (cost), cache each
  pair's embedding alongside the session message in `sessions.json` (or an
  in-memory dict keyed by session_id + turn index) — compute once when the
  pair is first added, reuse on subsequent prunes.

**Edge Cases:**
- If `len(messages) <= 2 + RECENCY_ANCHOR*2`, no pruning needed regardless of
  strategy.
- `current_query=""` (internal sessions / non-chat calls) → fall back to FIFO.
- Don't drop a pair that's part of an in-progress tool-call sequence (if any
  message has `role == "tool"`, keep it paired with its preceding
  assistant/tool-call message — check existing message structure for tool-call
  representation before implementing).

**Verification:** construct a synthetic 20-turn history where turn 3 contains
a remembered fact and turn 15 is topically related to a new query about a
*different* topic from turns 16-19; confirm semantic pruning keeps turn 3 and
the recency anchor, drops unrelated middle turns, and `MAX_CONTEXT_TOKENS` is
respected. Add a feature flag rollback test (`PRUNING_STRATEGY=fifo` reproduces
old exact behavior).

---

### Phase 24 — Background Continuous Visual Episodic Memory

**⚠ Privacy-sensitive — confirm with user before enabling by default. Ship
with `SCREEN_RECORDING_ENABLED=false`.**

**Goal:** Periodic desktop screenshot → OCR → store searchable text in a new
ChromaDB collection `screen_history`.

**New/Changed Files:**
- New `src/vision/screen_recorder.py`
- Edit `src/memory/chroma_db.py`: add `COL_SCREEN_HISTORY = "screen_history"`
  to the collection tuple in `_init_collections()`; add
  `add_screen_capture(text, app_name, window_title, timestamp)` and
  `search_screen_history(query, ...)` methods (mirror `find_facts` pattern).
- Edit `src/core/config.py`: `screen_recording_enabled: bool = False`,
  `screen_capture_interval_sec: int = 60`,
  `screen_recording_retention_days: int = 14`,
  `screen_recording_app_blocklist: list[str] = [...]` (1Password, Bitwarden,
  banking app names, browser private-window titles containing "Private"/
  "Incognito").
- New scheduler job registration (apscheduler — find existing job
  registration pattern, likely in `src/main.py` startup or a `scheduler`
  module).
- New API: `src/api/vision.py` or extend existing vision API — endpoints to
  toggle on/off, view retention settings, manually purge.
- HUD: toggle in settings panel (`static/js/panels.js` + `hud.css`).

**Interfaces:**
```python
# src/vision/screen_recorder.py
async def capture_and_index() -> Optional[dict]:
    """
    1. Get active window title/app name (platform-specific — Windows:
       win32gui or similar; check what's already used in src/system/control.py
       for any existing window-introspection code to reuse).
    2. If app_name/window_title matches blocklist (case-insensitive substring) → skip, return None.
    3. Capture screen via mss (already a dependency).
    4. OCR via pytesseract (add `pytesseract` to requirements; user must have
       Tesseract binary installed — document this).
    5. If OCR text is empty/whitespace → skip (avoid indexing blank captures).
    6. await chroma.add_screen_capture(text, app_name, window_title, time.time())
    7. Enforce retention: delete entries older than retention_days (run this
       check periodically, not every capture — e.g. once/hour).
    """
```

**Integration Points:**
- `RAGPipeline`: add `retrieve_screen_context()` analogous to
  `retrieve_knowledge()`, but **gated** — only call when intent/query
  explicitly references something visual ("what was I looking at",
  "earlier on my screen", "that document I had open") — do NOT make this
  always-on like `retrieve_knowledge()` (too noisy + privacy-sensitive to
  surface unprompted).

**Edge Cases:**
- Screen locked / idle → skip capture (check idle time via existing
  `pyautogui`/`psutil` if available).
- Multi-monitor: capture all monitors via `mss`, OCR each separately, store as
  separate documents tagged with monitor index.
- Tesseract not installed → log a clear one-time warning and disable the
  feature gracefully (don't crash startup).
- Storage growth: log collection size weekly; if it exceeds a configurable cap
  (e.g. 50k entries), purge oldest beyond retention regardless of age setting.

**Verification:** with `SCREEN_RECORDING_ENABLED=true` in a test env, confirm:
blocklisted app → no capture; normal app → OCR text indexed and retrievable
via `search_screen_history`; toggle off → job stops cleanly; retention purge
removes entries older than the configured window.

---

### Phase 25 — Local Speaker Biometrics & Voice-Keyed Authorization

**Goal:** Enroll a voice profile; verify speaker on each wake-word
activation; gate destructive commands behind a match.

**New/Changed Files:**
- New `src/voice/biometrics.py`
- Edit `src/voice/wake_word.py` (after VAD captures utterance, run
  verification before/alongside transcription)
- Edit `src/intelligence/commands.py` (add a `DESTRUCTIVE_COMMANDS` gate)
- Edit `src/system/executor.py` (code execution requires `voice_verified` OR
  text-based session — see edge cases)
- New API: `src/api/voice.py` — `/api/voice/enroll` (POST audio samples),
  `/api/voice/profile/status`, `/api/voice/profile/reset`.
- `requirements.txt`: add `resemblyzer` (pulls in `torch` — check if torch is
  already a transitive dep via sentence-transformers; likely yes, so minimal
  added weight).

**Interfaces:**
```python
# src/voice/biometrics.py
class VoiceProfile:
    PROFILE_PATH = Path("memory/cache/voice_profile.npy")
    SIM_THRESHOLD = 0.75  # cosine similarity, tune empirically

    @classmethod
    def enroll(cls, audio_samples: list[np.ndarray], sample_rate: int) -> bool:
        """Compute Resemblyzer embeddings for each sample, average, save.
        Require >= 3 samples of >= 2 seconds each."""

    @classmethod
    def is_enrolled(cls) -> bool: ...

    @classmethod
    def verify(cls, audio: np.ndarray, sample_rate: int) -> tuple[bool, float]:
        """Returns (matched, similarity). If not enrolled, returns (True, 1.0)
        — fail OPEN when no profile exists, so this is purely additive and
        never locks anyone out by default."""
```

**Integration Points:**
- `wake_word.py`: after capturing the utterance audio buffer (before/parallel
  to STT), call `VoiceProfile.verify(audio, sr)` → attach
  `voice_verified: bool` to the request context dict passed into
  `orchestrator.chat()`/`chat_stream()`.
- For **text-based** sessions (HUD chat input, API calls) there's no audio —
  define `voice_verified = None` (n/a) for text, and treat `None` as **NOT**
  satisfying the gate (destructive commands via text require a fallback PIN —
  see below) OR treat text-channel as inherently trusted (it required device
  access already). **Decide explicitly and document — recommend: text channel
  = trusted (already requires being logged into the HUD), gate only applies
  to voice channel.**
- `commands.py`: define
  ```python
  DESTRUCTIVE_COMMANDS = {"delete_file", "shutdown_system", "send_whatsapp",
                           "send_email", "execute_code", "modify_automation"}
  ```
  Before dispatching any command in this set from a **voice** session where
  `voice_verified is False`, return a persona-flavored refusal + offer the PIN
  fallback.
- Fallback PIN: store a hashed PIN (`src/core/config.py` or a separate
  encrypted file) — `verify_pin(spoken_or_typed_pin) -> bool`.

**Edge Cases:**
- Enrollment requires explicit user action (not automatic) — expose via HUD
  settings, not voice command (avoid "anyone says 'enroll my voice'" attack).
- Re-enrollment overwrites the profile — require confirmation.
- Background noise / multiple speakers: Resemblyzer embeddings degrade with
  noise — document a recommended quiet-room enrollment process; don't over-
  promise security (this is a convenience gate, not a security boundary —
  state this explicitly in user-facing docs).

**Verification:** enroll with 3 samples, verify same-speaker utterance passes
threshold, verify a different speaker (or TTS playback of enrolled phrase from
a different "voice") fails; confirm fail-open when no profile exists; confirm
destructive command via text channel is unaffected.

---

### Phase 26 — Real-Time Audio Streaming & Duplex Interruption

**Recommend splitting into 26a (streaming playback) and 26b (interruption).**
This is the largest phase — treat each sub-phase as its own implementation
session.

#### 26a — Streaming TTS Playback (no interruption yet)

**New/Changed Files:**
- New `src/api/voice_ws.py` — WebSocket endpoint `/ws/voice`
- Edit `src/voice/tts.py` — add a generator-based interface
  `synthesize_stream(text: str) -> AsyncIterator[bytes]` that yields audio
  chunks per sentence (split text on sentence boundaries via existing
  text-splitting if present, else simple regex on `. ! ? \n`)
- New `static/js/voice_stream.js` — client-side: opens WebSocket, receives
  binary audio chunks, queues via Web Audio API `AudioBufferSourceNode` chain
  for gapless playback
- Edit `static/index.html` to load the new script; edit `hud.js`/`voice.js`
  to route voice responses through the WS path when available, fall back to
  current SSE/REST flow otherwise (feature-flagged).

**Interfaces:**
```python
# src/api/voice_ws.py
@router.websocket("/ws/voice")
async def voice_ws(websocket: WebSocket):
    await websocket.accept()
    session_id = ... # from query param or first message
    while True:
        msg = await websocket.receive_text()  # user message (or audio→STT result)
        async for chunk in orchestrator.chat_stream_voice(session_id, msg):
            # chunk: {"type": "text_delta"|"audio_chunk"|"done", "data": ...}
            if chunk["type"] == "audio_chunk":
                await websocket.send_bytes(chunk["data"])
            else:
                await websocket.send_json(chunk)
```
- `orchestrator.chat_stream_voice()`: new method wrapping `chat_stream()` —
  as text deltas accumulate into complete sentences, pass each sentence to
  `tts.synthesize_stream()` and yield audio chunks interleaved with text.

**Edge Cases:**
- Sentence-splitting must handle abbreviations ("Mr.", "e.g.") reasonably —
  reuse any existing text-cleanup used before TTS in `tts.py` if present.
- Network hiccups: client buffers should handle out-of-order or delayed
  chunks gracefully (sequence numbers in chunk metadata).
- Kokoro ONNX vs edge-tts: confirm both can produce streamable per-sentence
  audio (Kokoro is local/fast — likely fine; edge-tts is a cloud fallback,
  per-sentence calls add latency — document this tradeoff).

#### 26b — VAD-Based Interruption (barge-in)

**New/Changed Files:**
- Edit `static/js/voice_stream.js` — continuous mic VAD (e.g.
  `@ricky0123/vad-web` via CDN, or simple energy-threshold VAD) running while
  TTS playback is active.
- Edit `src/api/voice_ws.py` — handle `{"type": "interrupt"}` client message:
  cancel the in-flight `chat_stream_voice()` generator (asyncio task
  cancellation), stop further TTS chunk generation.
- Edit `orchestrator.py` — when interrupted, store **only the portion of the
  assistant response that was actually spoken** (track via chunk index /
  character offset) into session history, not the full generated text.

**Edge Cases:**
- Echo cancellation: client mic will pick up the TTS output itself — VAD must
  distinguish user speech from echo (browser's built-in
  `echoCancellation: true` constraint on `getUserMedia`, plus a brief
  "cooldown" window right after TTS starts before VAD interruption is armed).
- Race condition: interrupt arrives just as `chat_stream_voice()` completes
  naturally — handle gracefully (no-op).
- Reconnection: WS drops mid-response — client should be able to resume/replay
  from last acknowledged chunk index, or just gracefully restart.

**Verification (26a+26b):** manual testing required (audio hardware) — provide
a test checklist: (1) ask a multi-sentence question, confirm audio plays
incrementally not after full generation; (2) interrupt mid-sentence by
speaking, confirm playback stops within ~300ms and history reflects only
spoken portion; (3) network blip during playback doesn't crash the session.

---

### Phase 27 — Ephemeral Docker Container Code Sandbox

**Requires Docker Desktop (WSL2 backend) on the host for runtime testing.**

**New/Changed Files:**
- New `src/system/docker_sandbox.py`
- Edit `src/system/executor.py` — `execute_python`/`execute_python_safe`/
  `execute_js`/`execute_bash` route through Docker when available, fall back
  to existing AST-sandboxed subprocess otherwise.
- `requirements.txt`: add `docker` (Docker SDK for Python).
- `src/core/config.py`: `code_sandbox_mode: str = "auto"` (`"docker" |
  "subprocess" | "auto"`), `docker_mem_limit: str = "256m"`,
  `docker_cpu_quota: float = 0.5`, `docker_timeout_sec: int = 30`,
  `docker_network_mode: str = "none"`.

**Interfaces:**
```python
# src/system/docker_sandbox.py
_IMAGES = {"python": "python:3.12-slim", "javascript": "node:20-slim",
           "bash": "python:3.12-slim"}  # bash via slim image's /bin/sh

class DockerSandbox:
    def __init__(self): self._client = docker.from_env()  # raises if Docker unreachable

    @classmethod
    def is_available(cls) -> bool:
        """Cached check — ping Docker daemon, cache result + recheck every 60s."""

    async def run(self, language: str, code: str, timeout: int = 30,
                   network: bool = False) -> dict:
        """
        1. Write `code` to a temp dir (per-execution scratch dir, cleaned up after).
        2. Run container:
           self._client.containers.run(
               image=_IMAGES[language],
               command=[...],
               volumes={scratch_dir: {"bind": "/work", "mode": "ro"}},  # read-only mount
               working_dir="/work",
               mem_limit=settings.docker_mem_limit,
               nano_cpus=int(settings.docker_cpu_quota * 1e9),
               pids_limit=64,
               network_mode="none" if not network else "bridge",
               read_only=True,
               tmpfs={"/tmp": "size=64m"},
               remove=True,
               detach=False,
               stdout=True, stderr=True,
               timeout=timeout,  # via subprocess-level timeout around .run() in executor
           )
        3. Return {"stdout": ..., "stderr": ..., "exit_code": ..., "timed_out": bool}
        """
```

**Integration Points:**
- `executor.py`: keep `_ast_scan` as a **first-pass filter** — reject
  obviously malicious code (matching existing `_BLOCKED_MODULES`/
  `_DANGEROUS_IMPORTS`) before even reaching Docker (defense in depth, and
  avoids spinning up containers for trivially-rejectable code).
- At module load / first use, call `DockerSandbox.is_available()`. If
  `code_sandbox_mode == "auto"`: use Docker if available, else fall back to
  current subprocess approach with `log.warning("[executor] Docker unavailable
  — using subprocess sandbox (reduced isolation)")`.
- `_kill_proc()` timeout-handling logic conceptually carries over — Docker SDK
  `.run(timeout=...)` + explicit container `.kill()`/`.remove()` on timeout
  (wrap in try/finally).

**Edge Cases:**
- First run pulls images (`python:3.12-slim`, `node:20-slim`) — can take
  minutes; pre-pull during setup, or pull-on-first-use with a clear log
  message (don't let it silently hang the first code execution).
- Read-only root + tmpfs `/tmp`: code that tries to write to `/work` will fail
  — confirm this matches expected sandbox semantics (scratch outputs should go
  to `/tmp`, document this for any agent code generating "save to file" code).
- Docker daemon becomes unavailable mid-session (e.g. user quits Docker
  Desktop) — `is_available()` recheck should catch this within 60s and fall
  back gracefully, not crash in-flight executions retroactively.

**Verification:** run identical test suite against both subprocess and Docker
backends (existing executor tests if any, else write new ones): basic
print/compute, network-blocked import attempt (`urllib`/`requests` → should
fail/timeout under `network_mode="none"`), infinite-loop timeout enforcement,
memory-bomb rejection (`mem_limit` triggers OOM kill), filesystem write
attempt outside `/tmp` (should fail under `read_only=True`).

---

### Phase 28 — Proactive Cron Analytics & Diagnostic Self-Healing ✅ IMPLEMENTED

**Status:** Implemented and verified (`tests/test_phase28_self_healing.py`,
74/74 checks pass). All interfaces below were implemented as specified:
`HealthTracker.get_status_summary()`, `SmartRouter.bias_fallback_chain()` /
`bias_status()` / `_check_bias_revert()` (TTL + recovery-based auto-revert) in
`router.py`; `run_health_check()`, `_pick_healthy_model()`,
`_log_diagnostic()` / `get_diagnostics_log()`, `_clear_memory_caches()`,
`_purge_old_data()` in new `src/system/self_healing.py`;
`ChromaManager.delete_old_conversations()` in `chroma_db.py` (only ever
touches `COL_CONVERSATIONS`, never `COL_KNOWLEDGE`/`COL_DOCUMENTS`/
`COL_EPISODES`); new `src/api/system_health.py`
(`/api/system/health/status|diagnostics|check`) registered in `main.py`
alongside a `self_healing_check` scheduler job gated by
`SELF_HEALING_ENABLED` (default on, opt-out no-op via
`SELF_HEALING_ENABLED=false`); new "System Health" HUD card in
`static/js/panels.js`; new settings `SELF_HEALING_ENABLED`,
`SELF_HEALING_INTERVAL_SEC`, `RAM_THRESHOLD_PCT`, `DISK_THRESHOLD_PCT`,
`CIRCUIT_TRIP_REORDER_THRESHOLD` added to `config.py` / `.env.example`.
Anti-thrashing state machine (RAM/disk/bias each tracked independently,
"condition persists" logged instead of repeating an action) and the
`screen_history` purge step were N/A since Phase 24 was excluded from this
build (no `screen_history` collection exists).

**New/Changed Files:**
- New `src/system/self_healing.py`
- Edit `src/intelligence/router.py` — expose `HealthTracker` state via a query
  method (e.g. `HealthTracker.get_status_summary() -> dict` and a way to
  temporarily reorder/bias `SmartRouter`'s fallback chain)
- Edit `src/analytics/collector.py` — expose cache-clear hooks
- New scheduler job (apscheduler — find existing registration site, likely
  `src/main.py` startup event or a dedicated `src/core/scheduler.py`)
- New `src/api/system_health.py` (or extend existing `src/api/system.py`) +
  HUD "System Health" card (`static/js/panels.js`, `hud.css`)
- `src/core/config.py`: `SELF_HEALING_ENABLED=true`,
  `SELF_HEALING_INTERVAL_SEC=300`, `RAM_THRESHOLD_PCT=85`,
  `DISK_THRESHOLD_PCT=90`, `CIRCUIT_TRIP_REORDER_THRESHOLD=3`

**Interfaces:**
```python
# src/system/self_healing.py
async def run_health_check() -> dict:
    """
    1. stats = psutil — cpu_percent, virtual_memory().percent, disk_usage('/').percent
    2. router_status = router.health_tracker.get_status_summary()
       # {"tripped_models": [...], "trip_counts": {...}}
    3. actions_taken = []
    4. if stats['ram'] > RAM_THRESHOLD_PCT:
         - clear analytics in-memory caches (collector.py hook)
         - gc.collect()
         - actions_taken.append("cleared_ram_caches")
    5. if stats['disk'] > DISK_THRESHOLD_PCT:
         - purge logs older than N days
         - purge screen_history (Phase 24) beyond retention
         - actions_taken.append("purged_disk")
    6. if len(router_status['tripped_models']) >= CIRCUIT_TRIP_REORDER_THRESHOLD:
         - router.bias_fallback_chain(prefer="healthy")  # temporary reorder
         - actions_taken.append("reordered_fallback_chain")
    7. log to memory/cache/diagnostics.jsonl
    8. return {"timestamp": ..., "stats": stats, "router_status": router_status,
               "actions_taken": actions_taken}
    """
```

**Integration Points:**
- `router.py`: add `HealthTracker.get_status_summary()` (read-only — iterate
  `_failures`/`_tripped_at`). Add `SmartRouter.bias_fallback_chain(prefer:
  str)` that temporarily reweights chain ordering (store an override dict with
  a TTL — e.g. 30 min — after which it auto-reverts to `models.json` defaults
  once tripped models recover, checked via `HealthTracker.is_available()`).
- This phase should run even for `_is_internal` triggers — it's a background
  job, not a chat hook; register via apscheduler, not via
  `orchestrator.chat()`.

**Edge Cases:**
- Don't purge `COL_KNOWLEDGE` (remembered facts) under any disk-pressure
  scenario — only `COL_CONVERSATIONS` (oldest first, beyond a configurable cap
  like 10k entries) and `screen_history`.
- Avoid thrashing: if an action was taken in the last interval and didn't
  resolve the condition, don't repeat it every cycle — log
  "condition persists after <action>" instead of repeating.
- `bias_fallback_chain` reorder must not violate provider-specific safe-param
  filtering already in `router.py` — it only reorders within the existing
  validated chain, doesn't add new providers.

**Verification:** mock `psutil` to report high RAM/disk, confirm
`run_health_check()` triggers expected actions and logs them; mock
`HealthTracker` with 3+ tripped models, confirm chain reorder + auto-revert
after tripped models recover (`mark_success` called).

---

### Phase 29 — Local Embedding Offloading & Ollama Mesh Fallback ✅ IMPLEMENTED

**Status:** Implemented and tested (56/56 checks pass in
`tests/test_phase29_ollama_fallback.py`; existing suites — Phase 22 (52/52 +1
skip), 23 (30/30), 28 (74/74 +1 skip), 34 (106/106) — re-run with no
regressions).

- `src/core/config.py`: added `ollama_fallback_enabled: bool = True`,
  `ollama_health_check_interval_sec: int = 60`, and (beyond spec, low
  priority item from Integration Points) `embedding_backend: str =
  "sentence_transformers" | "ollama"`.
- `src/intelligence/router.py`: added `_check_ollama_health()` (GET
  `{ollama_base_url}/api/tags`, 2s timeout, cached per
  `ollama_health_check_interval_sec` via `httpx.AsyncClient`); added
  `_OLLAMA_INTENT_MAP` (all 14 `models.json` categories →
  `config/models.json`'s 7 ollama models) and `_OLLAMA_FALLBACK_PRIORITY`
  ordered fallback list; replaced the final `raise
  AllProvidersExhaustedError(...)` with a fallback block that tries the
  intent-mapped model, falls through `_OLLAMA_FALLBACK_PRIORITY` on error
  (skipping models tripped via `HealthTracker.is_available()`), prepends
  `_LOCAL_FALLBACK_DISCLAIMER` to the response (handles both streaming and
  non-streaming), and records success/failure on `HealthTracker`. If Ollama
  is unreachable or `ollama_fallback_enabled=False`, the original
  `AllProvidersExhaustedError` still raises — no regression.
- `src/memory/embeddings.py`: documented the embedding-stays-local behavior
  in the module docstring; added `_embed_ollama()` (sync `httpx.Client` →
  `{ollama_base_url}/api/embeddings`) and wired `embed()` to dispatch on
  `settings.embedding_backend` (default unchanged:
  SentenceTransformers/`all-MiniLM-L6-v2`).
- New `scripts/check_ollama.py`: diagnostic — reports reachability, lists
  installed models, diffs against `config/models.json`'s ollama `_models`
  and prints `ollama pull <model>` for any missing.
- **Reconciliation**: HealthTracker is a 3-strike circuit breaker
  (`failure_threshold=3`); a single fallthrough failure records one strike
  via `mark_failure()` but doesn't trip `is_available()` to `False` by
  itself — the fallthrough test checks the failure was recorded, not that
  the breaker tripped, consistent with the existing Phase 28 design.
- Real end-to-end run against a live local Ollama instance is still pending
  (Vishnu's machine has Ollama installed/running per §1 of the progress
  handoff); all current verification is via mocked `httpx`/`_try_model`.

**Head start exists**: `config/models.json` already has `"ollama"` provider
with 7 models (`ollama/llama3.2`, `ollama/llama3.1`, `ollama/codellama`,
`ollama/deepseek-coder-v2`, `ollama/qwen2.5`, `ollama/mistral`,
`ollama/phi3`); `src/core/config.py` already has `ollama_base_url`. **Requires
Ollama running locally to actually exercise the fallback.**

**New/Changed Files:**
- Edit `src/intelligence/router.py` — Ollama health check + fallback
  injection
- New `scripts/check_ollama.py`
- `src/core/config.py`: `ollama_fallback_enabled: bool = True`,
  `ollama_health_check_interval_sec: int = 60`

**Interfaces:**
```python
# router.py additions
async def _check_ollama_health(self) -> bool:
    """GET {ollama_base_url}/api/tags with short timeout (2s). Cache result +
    timestamp; recheck every ollama_health_check_interval_sec."""

# In the main routing/fallback loop (find where AllProvidersExhaustedError
# is currently raised):
if all_providers_exhausted and settings.ollama_fallback_enabled and await self._check_ollama_health():
    # pick an ollama model matching the requested intent's category
    # (map intent categories → ollama model names, e.g. "coding" → "ollama/deepseek-coder-v2")
    fallback_model = _OLLAMA_INTENT_MAP.get(intent, "ollama/llama3.2")
    log.warning(f"[router] All cloud providers exhausted — falling back to local {fallback_model}")
    # proceed with normal litellm call against fallback_model
else:
    raise AllProvidersExhaustedError(...)
```

```python
# scripts/check_ollama.py — standalone diagnostic
# 1. Check if `ollama` binary / API reachable at ollama_base_url
# 2. If not reachable: print install instructions (https://ollama.com/download)
# 3. If reachable: list installed models, compare against config/models.json's
#    ollama list, print `ollama pull <missing>` commands for any gaps
```

**Integration Points:**
- `embeddings.py` — confirm/document it stays on local SentenceTransformers
  (`all-MiniLM-L6-v2`) regardless of this phase; this phase is about LLM
  *generation* fallback, not embeddings (embeddings are already 100% local).
  Optionally add a config flag `embedding_backend: str = "sentence_transformers"
  | "ollama"` for users who want to swap to an Ollama embedding model — low
  priority, document as optional.

**Edge Cases:**
- Ollama reachable but requested model not pulled → `litellm` call will fail;
  catch this specific error and either (a) attempt `ollama pull` automatically
  (slow, may not be desired) or (b) fall through to next available
  `_OLLAMA_INTENT_MAP` model, logging clearly which models are missing.
- Local fallback responses are likely lower quality — prepend a brief
  persona-appropriate disclaimer to the response when a local fallback was
  used (e.g. "(Running on local backup model — cloud providers are
  unavailable)") so the user knows.
- Health check must not block the hot path — run async with a short timeout
  and cache; never let an Ollama check itself become the reason for slow
  responses.

**Verification:** with Ollama NOT running, confirm `_check_ollama_health()`
returns False quickly (<2s) and `AllProvidersExhaustedError` still raises as
before (no regression). With Ollama running + `llama3.2` pulled, simulate all
cloud providers tripped (mock `HealthTracker`) and confirm fallback to
`ollama/llama3.2` succeeds with the disclaimer prepended.

---

### Phase 30 — Distributed Memory Mesh & Secured Mobile API Tunneling

**PWA + auth backend buildable now; Cloudflare/Tailscale tunnel registration
is user-side (their accounts/credentials) — deliver config templates +
setup guide.**

**New/Changed Files:**
- New `src/api/auth.py` — JWT issuance/verification middleware
- New `static/mobile/` — PWA (manifest.json, service worker, mobile-first
  chat UI reusing `static/js/chat.js` logic where possible)
- New `scripts/sync_memory_mesh.py`
- `requirements.txt`: add `pyjwt`, `passlib[bcrypt]` (or similar) if not
  present
- `src/core/config.py`: `auth_enabled: bool = False` (default off — opt-in
  for remote access only), `jwt_secret: str` (from `.env`, generate via
  `secrets.token_urlsafe(32)`), `jwt_expiry_minutes: int = 60`
- New `config/cloudflared_config.yml.example`, `docs/REMOTE_ACCESS_SETUP.md`

**Interfaces:**
```python
# src/api/auth.py
router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.post("/login")
async def login(credentials: LoginRequest) -> TokenResponse:
    """Verify against a single-user credential stored in .env (hashed).
    Issue short-lived JWT."""

# Dependency for protected routes:
async def require_auth(token: str = Depends(oauth2_scheme)) -> dict:
    """Verify JWT; raise 401 if invalid/expired."""

# In main.py: only apply require_auth dependency to routes when
# settings.auth_enabled — local-network access (no auth header) continues
# working unauthenticated when auth_enabled=False (default), so this is
# strictly additive for remote/tunnel use.
```

```python
# scripts/sync_memory_mesh.py
"""
Periodic export/import of COL_KNOWLEDGE + COL_EPISODES between two TRON-X
instances reachable over the tunnel:
  1. Export: dump collection (ids, documents, metadatas, embeddings) to JSON.
  2. Transfer: POST to peer's /api/memory/mesh/import (new protected endpoint).
  3. Import: peer upserts received entries (id-based dedup via existing
     _make_id hashing — already idempotent).
  4. Track last-sync timestamp per peer to only transfer deltas
     (filter by metadata.timestamp > last_sync).
"""
```

**Integration Points:**
- PWA talks to existing `/api/chat`, `/api/memory/*`, `/api/iot/*` endpoints —
  no new chat backend needed, just a new frontend + auth layer in front of it
  for non-local access.
- `manifest.json` + service worker: standard PWA installability (icons,
  `display: "standalone"`, offline shell caching static assets — chat data
  itself requires connectivity).

**Edge Cases:**
- `auth_enabled=False` by default — local HUD usage on the same machine is
  unaffected. Only enable when exposing via tunnel.
- JWT secret must be generated fresh per install, stored in `.env` (gitignored
  already), never hardcoded/committed.
- Memory mesh sync: handle conflicting facts (same content hash → naturally
  deduped via existing `_make_id`; differing content about the "same" topic
  is NOT auto-merged — out of scope, just dedupe by exact content hash).
- Tunnel config files are templates only (`*.example`) — actual credentials
  filled in by the user, not committed.

**Verification:** with `auth_enabled=true`, confirm `/api/chat` returns 401
without a valid JWT and 200 with one obtained via `/api/auth/login`; confirm
`auth_enabled=false` (default) leaves all routes unauthenticated (no
regression for local use); test `sync_memory_mesh.py` against two local
ChromaDB instances (different `CHROMA_PATH`) to confirm delta sync + dedup
works before requiring an actual tunnel.

---

### Phase 31 — Adaptive Persona & Preference Learning

**New/Changed Files:**
- Edit `src/intelligence/persona.py` (`build_system_prompt()` — inject a
  "user preferences" block)
- New `src/intelligence/preference_tracker.py`
- Storage: new metadata-tagged entries in `COL_KNOWLEDGE` (type:
  `"preference"`, distinct from `type: "fact"`) or a small JSON file
  `memory/cache/preferences.json` — JSON file is simpler since preferences
  are structured (verbosity, tone, formatting) not free-text facts.

**Interfaces:**
```python
# src/intelligence/preference_tracker.py
class PreferenceTracker:
    """
    Tracks implicit signals per session/long-term:
      - "no I meant..." / "actually..." corrections → topic miscue counter
      - response length vs. user's typical follow-up ("can you shorten that?")
      - explicit style commands ("be more concise", "stop using bullet points")
        — these likely already work via system prompt edits; this phase makes
        them PERSISTENT across sessions.
    """
    async def record_signal(self, session_id: str, signal_type: str, detail: dict): ...
    async def get_profile(self) -> dict:
        """Returns {"verbosity": "concise"|"normal"|"detailed",
                     "formatting": "minimal"|"normal", "tone": [...]}"""
    async def update_from_explicit_command(self, message: str) -> bool:
        """Detect 'be more concise', 'stop bolding everything', etc. via
        regex (similar style to memory_commands.py parsers) and persist
        immediately to preferences.json."""
```

**Integration Points:**
- `persona.py: build_system_prompt()` — append a "User Preferences" section
  built from `PreferenceTracker.get_profile()`, e.g. "The user prefers concise
  responses with minimal formatting" — analogous to how
  `RAG_CONTEXT_TEMPLATE` is appended for retrieved context.
- `commands.py` — wire `update_from_explicit_command()` similarly to memory
  commands (check before general chat, since "be more concise" is itself an
  instruction not meant to trigger an LLM call... though arguably it IS a
  valid chat message too — recommend: don't intercept as a command, instead
  always run `update_from_explicit_command()` as a side-effect check on every
  message, non-blocking, in addition to normal chat processing).

**Edge Cases:**
- Don't let inferred preferences override explicit per-message instructions
  ("just this once, give me the long version") — preferences are a *default*,
  explicit per-turn instructions in the current message should win (LLM
  naturally handles this if preferences are framed as "default style" rather
  than "always do X").
- Avoid preference drift from sarcasm/one-off comments — require the same
  signal N times (e.g. 3) before persisting, or require explicit "always"/
  "from now on" framing for instant persistence.

**Verification:** simulate a session where the user says "stop using bullet
points" → confirm preference persists to `preferences.json` and is reflected
in `build_system_prompt()` output for a brand-new session.

---

### Phase 32 — Browser Macro Recorder & Replay

**New/Changed Files:**
- Edit `src/agents/browser_agent.py` (Playwright-based — add recording mode)
- New `src/agents/macro_recorder.py`
- Storage: `memory/cache/macros/<macro_name>.json`
- Edit `src/agents/scheduler_agent.py` (allow scheduling a macro replay)
- New commands via `commands.py`: "record a macro called X", "stop recording",
  "run macro X [with <params>]"

**Interfaces:**
```python
# src/agents/macro_recorder.py
class MacroRecorder:
    async def start_recording(self, name: str) -> None:
        """Attach Playwright event listeners (page.on('framenavigated'),
        click/input listeners via injected JS) to record an action sequence."""

    async def stop_recording(self) -> dict:
        """Returns the macro definition:
        {"name": ..., "steps": [{"action": "goto"|"click"|"fill"|"select", 
          "selector": ..., "value": ..., "param_name": Optional[str]}, ...]}
        Save to memory/cache/macros/<name>.json"""

    async def replay(self, name: str, params: dict | None = None) -> dict:
        """Load macro, substitute {param_name} placeholders in 'value' fields
        from `params`, execute steps sequentially via browser_agent's existing
        Playwright page object. Return {"success": bool, "screenshot": ...,
        "error": Optional[str]}"""

    async def parametrize_step(self, macro_name: str, step_index: int, param_name: str) -> None:
        """Mark a recorded value as a parameter (e.g. the date field in a
        booking form) — exposed via a follow-up command:
        'in macro X, make step 3 a parameter called date'"""
```

**Edge Cases:**
- Recorded selectors (CSS/XPath) can break if the target site changes layout —
  on replay failure, capture a screenshot + error and report clearly rather
  than silently failing; suggest re-recording.
- Sites with login/2FA: macros should NOT record/replay credential entry —
  detect password-type fields and refuse to record their values (record the
  step as "MANUAL: enter password" placeholder requiring user intervention).
- Scheduled replay (via `scheduler_agent`) of a macro touching a real
  transaction (purchases, form submissions with side effects) should require
  the same "explicit permission" gating as any other purchase/submit action —
  this phase should default scheduled macros to **dry-run / notify-before-
  submit** unless the user explicitly marks a macro as "auto-submit".

**Verification:** record a macro on a simple test page (e.g. a search form),
replay with a different parameter value, confirm correct substitution and
result; test password-field detection refuses to record a login form's
password input.

---

### Phase 33 — Encrypted Memory Backup & Disaster Recovery ✅ IMPLEMENTED

**Status:** Implemented and tested (47/47 checks pass in
`tests/test_phase33_backup.py`; existing suites — Phase 21 (40/40), 22
(52/52 +1 skip), 23 (30/30), 28 (74/74 +1 skip), 29 (56/56), 34 (106/106) —
re-run with no regressions).

- New `src/system/backup.py`: Fernet (AES-128-CBC + HMAC-SHA256) encryption,
  key derived from `backup_passphrase` via PBKDF2-HMAC-SHA256 (480,000
  iterations, random 16-byte salt per archive, salt+version JSON header
  stored unencrypted ahead of the ciphertext). `create_backup()` snapshots
  `_BACKUP_PATHS` (`memory/chroma`, `memory/cache/sessions.json`,
  `memory/cache/voice_profile.npy` (Phase 25, not yet implemented),
  `memory/cache/preferences.json` (Phase 31, not yet implemented) —
  existence-checked, so those phases need no changes here) into a temp
  snapshot dir under `ChromaManager._lock` (fast copy), then tars +
  encrypts outside the lock (slow), per the spec's edge-case guidance.
  Writes `backup_dir/tronx_backup_<YYYYMMDDTHHMMSSZ>.tar.enc` and enforces
  `backup_retention_count` via `_enforce_retention()`. Also exports
  `decrypt_backup()` and `list_backups()`.
- New `scripts/restore_backup.py`: CLI (`<backup_file> --passphrase ...
  [--yes] [--target-root DIR]`) decrypts, lists archive contents and prompts
  for confirmation (unless `--yes`), copies any existing `memory/` to
  `memory_pre_restore/` as a safety net, then extracts.
- `src/main.py` lifespan: opt-in cron job `"encrypted_memory_backup"`
  registered via `scheduler.add_cron_job(..., cron_expr=settings.backup_cron)`
  when `settings.backup_enabled` (logs `[backup] Enabled -- ...` /
  `[backup] Disabled (BACKUP_ENABLED=false)` / a startup warning if enabled
  without a passphrase).
- `src/core/config.py`: `backup_enabled: bool = False`, `backup_dir: str =
  "backups/"`, `backup_retention_count: int = 7`, `backup_passphrase:
  Optional[str] = None`, `backup_cron: str = "0 3 * * *"`.
- `.env.example` and `.gitignore` updated (`backups/`, `memory_pre_restore/`
  gitignored).

**Reconciliation vs. spec:** the spec proposed `backup_enabled: bool = True`
+ "fail loudly at startup" if `backup_passphrase` is missing while enabled.
Implemented as **opt-in** (`backup_enabled=False` default) instead, with a
non-fatal startup *warning* (backups stay disabled) if enabled without a
passphrase. Rationale: existing installs have no `BACKUP_PASSPHRASE`
configured, so default-on would either crash startup (regression for every
current install) or silently no-op (defeating "fail loudly"). Opt-in
preserves current behavior for everyone, and matches this codebase's
established "never crash startup, degrade to no-op" convention (Phase 22
`IntentCache.enabled`, Phase 28 `self_healing_enabled`). Full rationale is
documented in `src/system/backup.py`'s module docstring.

**New/Changed Files:**
- New `src/system/backup.py`
- New `scripts/restore_backup.py`
- Scheduler job registration (apscheduler — daily/weekly)
- `src/core/config.py`: `backup_enabled: bool = True`,
  `backup_dir: str = "backups/"`, `backup_retention_count: int = 7`,
  `backup_passphrase: str` (from `.env`, required if `backup_enabled`)

**Interfaces:**
```python
# src/system/backup.py
async def create_backup() -> Path:
    """
    1. Create a tar archive of:
       - memory/chroma/  (ChromaDB persistent store)
       - memory/cache/sessions.json
       - memory/cache/voice_profile.npy (if exists, Phase 25)
       - memory/cache/preferences.json (if exists, Phase 31)
       - WhatsApp bridge auth state (find existing path — likely
         memory/cache/whatsapp_session/ or similar)
    2. Encrypt the tar via `age` (recommend over gpg — simpler, modern,
       single-binary; add as documented external dependency, OR use a
       pure-python option like `cryptography`'s Fernet with a key derived
       from backup_passphrase via PBKDF2 if avoiding external binaries is
       preferred — RECOMMEND Fernet/cryptography for zero external deps).
    3. Write to backup_dir/tronx_backup_<timestamp>.tar.enc
    4. Enforce backup_retention_count — delete oldest beyond limit.
    5. Return path to new backup.
    """

# scripts/restore_backup.py
"""
CLI: python scripts/restore_backup.py <backup_file> [--passphrase ...]
1. Decrypt archive.
2. Confirm with user (print contents/timestamp) before overwriting current
   memory/ — require explicit --yes flag or interactive confirmation.
3. Extract to memory/ (after backing up CURRENT memory/ to memory_pre_restore/
   as a safety net).
"""
```

**Edge Cases:**
- Never run backup while a ChromaDB write is in-flight — acquire
  `ChromaManager._lock` (or a coarser app-level lock) during the file-copy
  portion to avoid a torn snapshot. Since ChromaDB is file-based (DuckDB/
  SQLite under the hood depending on version), a simple approach: copy to a
  temp dir under the lock (fast), then tar/encrypt the copy outside the lock
  (slow) — minimizes lock hold time.
- `backup_passphrase` missing when `backup_enabled=true` → fail loudly at
  startup with a clear config error, don't silently skip backups.
- Backup directory should itself be excluded from the project's normal git
  tracking (add `backups/` to `.gitignore`) and ideally point outside the repo
  entirely by default (or at least clearly gitignored) since it contains all
  memory data.

**Verification:** create a backup, corrupt/delete `memory/chroma/`, restore
from backup, confirm `chroma.stats()` shows the same collection counts as
before corruption. Confirm retention deletes oldest backups beyond
`backup_retention_count`.

---

### Phase 34 — Unified Cost & Usage Dashboard ✅ IMPLEMENTED

**Status:** Implemented and verified (`tests/test_phase34_cost_dashboard.py`,
106/106 checks pass). New `config/pricing.json` holds per-provider defaults,
model-specific overrides, and `:free`-suffix detection. `collector.py` gained
`_load_pricing()` (cached, safe fallback), `_price_for_model()` (resolution
order: model_overrides -> free_suffix_markers -> provider_defaults ->
unpriced), `_parse_period_days()`, `_circuit_breaker_events_since()` (Phase 28
diagnostics integration with graceful degradation), and
`get_cost_summary(period)` returning total/by-provider/by-model/by-intent
costs, token totals, `unpriced_models`, `pricing_last_updated`,
`cache_hit_rate` (`None`, reserved for Phase 22), and
`circuit_breaker_events`. `prompt_tokens`/`completion_tokens` now flow
end-to-end from `orchestrator.py` -> `chat.py` -> `record_chat()`, with an
automatic schema migration (`_migrate_chat_events_columns`) for pre-existing
`chat_events` tables. New `GET /api/analytics/dashboard` endpoint
(`src/api/analytics.py`) and a `renderCostDashboard()` HUD card in
`panels.js`, wired to a new `cost` intent (cost/spending/billing/budget/token
usage phrasing), positioned ahead of the generic crypto/system regexes to
avoid "token"/"usage" collisions.

**New/Changed Files:**
- Edit `src/analytics/collector.py` (493 lines — likely already tracks
  per-call provider/model/tokens; add cost calculation if not present)
- New `src/api/analytics.py` endpoint(s) for dashboard data (extend existing
  `src/api/analytics.py` which already exists at `/api/analytics`)
- New HUD card in `static/js/panels.js` + `hud.css`

**Interfaces:**
```python
# collector.py — confirm/add:
PROVIDER_COST_PER_1K_TOKENS = {  # from config/models.json or a new
                                  # config/pricing.json — costs change often,
                                  # keep in a separate JSON for easy updates
    "groq/...": {"input": 0.0, "output": 0.0},  # many free-tier
    "openrouter/...": {...},
    # ...
}

class AnalyticsCollector:
    def get_cost_summary(self, period: str = "7d") -> dict:
        """{"total_cost_usd": ..., "by_provider": {...}, "by_model": {...},
            "by_intent": {...}, "cache_hit_rate": ...,  # Phase 22
            "circuit_breaker_events": [...]}"""  # Phase 28
```

**Integration Points:**
- New `/api/analytics/dashboard` endpoint returning the above summary,
  consumed by a new HUD card (`showCard("Cost & Usage", renderCostDashboard,
  pollMs=60000)` following the existing card pattern in `panels.js`).
- Cache-hit rate (Phase 22) and circuit-breaker history (Phase 28) are
  optional fields — implement this phase's core (cost/usage) independent of
  those phases, add the extra fields when/if 22 and 28 land.

**Edge Cases:**
- Pricing data goes stale — add a `pricing_last_updated` field surfaced in the
  dashboard so the user knows if costs might be inaccurate; treat free-tier
  providers (groq, etc.) as $0 explicitly rather than omitting them.
- Large history: aggregate incrementally (don't recompute from all-time raw
  logs on every dashboard poll) — maintain rolling daily aggregates.

**Verification:** seed `collector.py` with synthetic call records across 3
providers/models, confirm `get_cost_summary()` totals match manual
calculation; confirm dashboard endpoint responds in <200ms even with a large
history (aggregation, not full scan).

---

### Phase 35 — Automation Rules Engine

**New/Changed Files:**
- New `src/intelligence/automation_engine.py`
- Storage: `memory/cache/automations.json`
- Edit `src/iot/nl_mapper.py` (expose action-resolution as a reusable function
  callable from the automation engine, not just from chat)
- Edit `src/agents/scheduler_agent.py` (time-based triggers) and
  `src/agents/reminder_agent.py` (overlap — reconcile: reminders are
  user-facing notifications, automations are silent actions; keep distinct)
- New commands via `commands.py`: "when I say X, do Y", "list automations",
  "disable automation X"
- New API: `src/api/automations.py`

**Interfaces:**
```python
# src/intelligence/automation_engine.py
@dataclass
class Automation:
    id: str
    name: str
    trigger: dict   # {"type": "voice_phrase"|"time"|"iot_state"|"calendar_event",
                     #  "value": ...}
    conditions: list[dict]  # optional, e.g. [{"type": "time_range", "after": "18:00"}]
    actions: list[dict]     # [{"type": "iot"|"reminder"|"macro"|"chat_command", "params": {...}}]
    enabled: bool = True

class AutomationEngine:
    async def parse_from_nl(self, message: str) -> Optional[Automation]:
        """LLM-assisted parse of 'when I say goodnight, turn off all lights and
        arm security mode' into an Automation object. Use a structured-output
        prompt similar to task_decomposer.py's planner."""

    async def check_voice_triggers(self, message: str) -> list[Automation]:
        """Match message against all enabled voice_phrase triggers (exact or
        fuzzy match — reuse intent_cache's embedding similarity if Phase 22
        landed, else simple normalized string match)."""

    async def check_time_triggers(self) -> list[Automation]:
        """Called by scheduler — return automations whose time trigger fires now."""

    async def check_iot_state_triggers(self, entity_id: str, new_state: Any) -> list[Automation]:
        """Called when IoT state changes (if IoT integration supports
        webhooks/polling for state changes)."""

    async def execute(self, automation: Automation) -> dict:
        """Run conditions check, then execute each action via the relevant
        existing handler (nl_mapper for IoT, reminder_agent for reminders,
        macro_recorder.replay for macros (Phase 32), commands dispatcher for
        chat commands)."""
```

**Integration Points:**
- `commands.py`: voice-phrase triggers checked early (similar position to
  memory commands) — if `check_voice_triggers()` returns matches, execute and
  return a confirmation; otherwise continue normal processing.
- IoT state-change triggers depend on the existing IoT integration's
  capabilities — check `src/iot/` for whether state changes are
  pushed/polled; if only polling exists, `check_iot_state_triggers` runs on
  the existing IoT polling cycle.

**Edge Cases:**
- Loop prevention: an automation's actions must not be able to trigger the
  same automation (or a cycle of automations) — track an execution-depth
  counter per trigger chain, cap at e.g. 3.
- Destructive actions within automations (Phase 25's `DESTRUCTIVE_COMMANDS`)
  should still be gated — automations created via voice should not silently
  bypass voice-biometric gating for destructive actions; require such
  automations be created/edited via the (trusted) text/HUD channel.
- `disable automation X` / `list automations` must be easy — automations that
  silently misfire are confusing; make them easy to audit and turn off.

**Verification:** create an automation via NL ("when I say goodnight, turn off
the lights"), confirm `parse_from_nl` produces correct `Automation`, confirm
`check_voice_triggers("goodnight")` ma`check_voice_triggers("goodnight")` matches and `execute()` calls the IoT
handler with correct params; test loop prevention with two automations that
reference each other.

---

### Phase 36 — Self-Tuning Router via A/B Feedback

**New/Changed Files:**
- Edit `src/intelligence/router.py` (extend existing A/B testing framework —
  find the Phase 3 A/B implementation referenced in the router's docstring:
  "A/B model testing framework with per-variant metrics")
- New `src/intelligence/router_feedback.py`
- Storage: extend whatever store the existing A/B framework already uses
  (check router.py for where A/B metrics are persisted — likely
  `memory/cache/` JSON or in `analytics/collector.py`)

**Interfaces:**
```python
# src/intelligence/router_feedback.py
class RouterFeedback:
    """
    Correlates A/B variant choice with downstream signals:
      - User immediately regenerates the response → negative signal
      - User edits/corrects the response heavily → negative signal
      - Response leads to a 'remember'/positive follow-up → neutral/positive
      - Latency (already tracked per Phase 3 P50 windows)
    """
    async def record_outcome(self, request_id: str, variant: str, intent: str,
                              signal: str, weight: float = 1.0) -> None:
        """signal in {"regenerated", "edited", "accepted", "slow", "error"}"""

    async def get_variant_scores(self, intent: str) -> dict[str, float]:
        """Rolling-window score per (intent, variant) — used to bias
        SmartRouter's variant-selection probability for that intent."""

    async def adjust_routing_weights(self) -> dict:
        """Periodically (e.g. hourly via scheduler) recompute weights and
        update SmartRouter's per-intent variant probabilities. Cap max shift
        signals. Log all adjustments to memory/cache/router_tuning.jsonl for
        audit + manual override."""
```

**Integration Points:**
- "Regenerate" signal requires the HUD to send a `regenerate` flag when the
  user clicks a regenerate button (check if this UI affordance exists in
  `static/js/chat.js` — if not, this phase has a small frontend component too:
  add a regenerate button that tags the request).
- Manual override: expose current variant weights + an admin endpoint to pin
  a model/variant for an intent (escape hatch if auto-tuning misbehaves).

**Edge Cases:**
- Cold start: with little feedback data, don't over-shift — require a minimum
  sample size (e.g. 20 outcomes) per (intent, variant) before adjusting away
  from the configured defaults.
- Don't let auto-tuning disable a model entirely based on noisy signals —
  combine with `HealthTracker` (genuine errors/circuit trips) as a stronger
  signal than soft signals (regenerate/edit).

**Verification:** simulate outcome records favoring variant B over variant A
for intent "coding" past the minimum sample threshold; confirm
`adjust_routing_weights()` shifts probability toward B within the capped
per-cycle limit, and that `router_tuning.jsonl` logs the adjustment with
before/after weights.

---

## 3. Sequencing (from TRONX_EVOLUTION_ROADMAP.md)

| Batch | Phases | Rationale |
|---|---|---|
| 1 | 23, 28, 34 | Backend/quality wins, build on existing `_trim_history`, `analytics/collector.py`. |
| 2 | 22, 29 | Performance + resilience; 29 has a head start (Ollama in catalog). |
| 3 | 21, 33 | Supervisor loop benefits from cleaner context (22/23); backup is cheap insurance. |
| 4 | 27 | Security-critical — do before remote access (30). |
| 5 | 25, 35 | Independent; can interleave. |
| 6 | 24 | Pending privacy review; default OFF. |
| 7 | 26 | Largest lift — 26a then 26b. |
| 8 | 30, 31, 32, 36 | Remote access + polish, after security/stability. |

## 4. General Verification Protocol (every phase)

1. `python3 -m py_compile <every changed .py file>` (use `outputs/verify/`
   workaround if bash mount appears stale — see §0.2).
2. For new regex/parsing logic: standalone test script with ≥10 hand-written
   cases (positive, negative, edge punctuation) — pattern from
   `memory_commands.py`'s 17-case harness.
3. For new config flags: confirm default values preserve current behavior
   (every new feature should be opt-in or no-op by default unless explicitly
   a bugfix).
4. For HUD/static changes: `node --check` on JS, manual screenshot/visual
   check, and confirm no box-drawing-character corruption (heredoc method).
5. Update `.env.example` for any new required env vars.
6. Update this doc / `TRONX_EVOLUTION_ROADMAP.md` with ✅ status and any
   deviations from plan, so the next session has an accurate state.
