# TRON-X → 10× Plan
### Structural analysis, issue resolution (incl. the HuggingFace cache fix), and a "2050-grade" feature roadmap
*Prepared 2026-06-11 · scope: full `src/` tree (99 files, ~23.8k LOC), config, infra, docs*

---

## 0. TL;DR

TRON-X is already a serious piece of engineering: a FastAPI personal-AI platform with multi-LLM routing across 104 models/14 providers, RAG + episodic memory, 16 agents, voice/vision/IoT/WhatsApp, a HUD, and 7 of 16 "advanced phases" shipped with passing test suites. It does **not** need a rewrite. It needs three things to become 10× better:

1. **Stop the bleeding** — one genuinely broken thing (the hardcoded HuggingFace cache path) plus two hygiene gaps (no git commits, a live `.env` in the tree) that put the whole project at risk.
2. **Refactor the spine** — turn the 99-file pile into 5 enforced layers with a typed event bus, so new capability stops requiring edits in six places.
3. **Cross the proactivity threshold** — the assistant currently *reacts*. Every ingredient for it to *anticipate* already exists. Wiring them into one predictive loop is the single highest-leverage move and the foundation for the "2050" features.

A note on the existing bug list: **`CODE_REVIEW_logical_errors.md` is now stale.** I verified against the live source — `forget_before()`, the subprocess-kill, the bash whitelist, the `allow_network` scoping, and the `nl_mapper` temperature/brightness bugs are all already fixed in the current code. Don't spend a cycle on them. The real outstanding issues are below.

---

## 1. Architecture as it stands

### 1.1 The map

```
run.py ──> src/main.py (FastAPI lifespan: router, orchestrator, scheduler, self-healing)
            │
            ├── src/api/          19 routers (chat, voice, agents, memory, iot, feeds, …)
            ├── src/intelligence/ orchestrator, router, intent(+cache), persona, cot, prompts
            ├── src/agents/       16 agents + coordinator + supervisor + task_decomposer
            ├── src/memory/       chroma_db, embeddings, episodic_memory, rag, supabase
            ├── src/voice/        stt, tts, vad, wake_word
            ├── src/vision/       screen
            ├── src/iot/          home_assistant, mqtt_client, ws_listener, nl_mapper
            ├── src/system/       executor, control, files, browser, backup, self_healing
            ├── src/feeds/        news, stocks, crypto, weather
            ├── src/analytics/    collector, middleware
            ├── src/plugins/      registry, manifest
            └── src/core/         config, auth, ratelimit, logger, exceptions
```

This is a clean *folder* taxonomy. What's missing is an enforced *dependency* taxonomy — see §3.

### 1.2 What's genuinely strong (keep, don't touch)

- **The router** (`intelligence/router.py`): failover, circuit-breaker, rate-limiting, A/B, and an Ollama local-mesh fallback. This is the crown jewel.
- **Memory layering**: Chroma (vector) + episodic + Supabase + RAG, with a local-embedding default that survives provider outages.
- **The phase discipline**: each advanced phase ships with a dedicated test file (`tests/test_phase2x_*.py`) and a written handoff. Seven phases, all green. This is rare and worth protecting.
- **Encrypted backup** (`system/backup.py`): Fernet + PBKDF2-HMAC-SHA256 at 480k iterations, per-archive salt, retention enforcement. Production-grade.
- **Self-healing cron** (`system/self_healing.py`): CPU/RAM/disk + router health, already wired into lifespan.

### 1.3 Maturity scorecard

| Dimension | Now | Target | Gap |
|---|---|---|---|
| Feature breadth | 9/10 | 10/10 | small |
| LLM routing/resilience | 9/10 | 10/10 | small |
| Memory architecture | 7/10 | 10/10 | knowledge graph, consolidation |
| Code-correctness (logic bugs) | 8/10 | 9/10 | mostly cleared already |
| **Reproducibility / portability** | **3/10** | 9/10 | **hardcoded paths, no lockfile** |
| **Version control hygiene** | **1/10** | 9/10 | **zero commits, live `.env`** |
| Test coverage | 5/10 | 9/10 | phase tests only; no core/api smoke |
| Proactivity | 2/10 | 9/10 | **the big unlock** |
| Observability | 5/10 | 9/10 | cost data partly discarded |

The two red rows are where "10×" actually lives. Breadth is already near the ceiling.

---

## 2. Issue resolution

### 2.1 THE cache issue — HuggingFace transformers cache (priority 1)

**Where:** `src/memory/embeddings.py:36–50`

```python
_HF_CACHE_ROOT = Path(r"D:\updated e drive")          # ← hardcoded, machine-specific
if _HF_CACHE_ROOT.exists():
    os.environ["HF_HOME"] = str(_HF_CACHE_ROOT)
    os.environ["HF_HUB_CACHE"] = str(_HF_CACHE_ROOT / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(_HF_CACHE_ROOT / "hub")
```

**Why it's a real problem, not cosmetic:**

1. **It's a path, baked into source, that already migrated once** (the comment says it moved from a "now-missing `E:\` drive"). It will break again on the next machine, the next reinstall, in Docker (`Dockerfile` exists), in CI, and for anyone who clones the repo. The `else` branch only logs a warning, so the failure mode is a confusing `FileNotFoundError` deep inside `sentence_transformers` at first embedding call — i.e. memory silently dies at runtime, not at startup.
2. **`TRANSFORMERS_CACHE` is deprecated** in modern `huggingface_hub`/`transformers` — it emits a warning and is superseded by `HF_HOME` + `HF_HUB_CACHE`. Setting all three invites split-brain caches where the model downloads to one dir and is read from another.
3. **It's set as an import side-effect**, so import order silently determines whether it takes effect. Any module that imports `sentence_transformers` before `embeddings` wins, unpredictably.

**The fix — config-driven, portable, fail-loud, no deprecated vars:**

Add to `src/core/config.py` (`Settings`):

```python
# HuggingFace / model cache. Resolution order:
#   1) explicit setting / env HF_HOME
#   2) existing process env (respect what the OS/container already set)
#   3) platform default: <project_root>/.cache/huggingface
hf_cache_dir: Optional[str] = Field(default=None)
```

New module `src/core/model_cache.py` (single source of truth, imported *first* in `main.py` and in `embeddings.py` before any HF import):

```python
import os
from pathlib import Path
from src.core.config import get_settings
from src.core.logger import log

def configure_hf_cache() -> Path:
    """Resolve and export the HF cache dir exactly once, portably."""
    s = get_settings()
    root = (
        Path(s.hf_cache_dir).expanduser() if s.hf_cache_dir
        else Path(os.environ["HF_HOME"]) if os.environ.get("HF_HOME")
        else Path(__file__).resolve().parents[2] / ".cache" / "huggingface"
    )
    root.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(root)
    os.environ["HF_HUB_CACHE"] = str(root / "hub")
    os.environ.pop("TRANSFORMERS_CACHE", None)   # drop the deprecated var
    log.info(f"[hf] cache → {root}")
    return root
```

Then in `embeddings.py`, replace the hardcoded block with:

```python
from src.core.model_cache import configure_hf_cache
configure_hf_cache()   # runs before the lazy `from sentence_transformers import …`
```

**Bonus robustness (recommended):** pin the embedding model offline so it never re-downloads on a flaky network — set `HF_HUB_OFFLINE=1` once the model is present, with a one-time warm-up script `scripts/warm_models.py` that calls `_get_model()` and exits. Then the `Dockerfile` runs that script at build time so the image ships with the 22 MB MiniLM baked in. This converts "downloads on first request, in the request's latency budget" into "already present."

**Acceptance test:** `tests/test_model_cache.py` — point `hf_cache_dir` at a tmpdir, assert env vars set correctly, assert `TRANSFORMERS_CACHE` is absent, assert `configure_hf_cache()` is idempotent, assert embeddings load and return a 384-dim vector.

### 2.2 Version control — repo has zero commits (priority 1)

`.git/` exists but `git log` errors with no commits. For a 24k-LOC project this means no history, no diffs, no rollback. **Before the first commit**, `.env` (6 KB of live keys) must be excluded — it currently is not in `.gitignore`.

Sequence:
1. Add to `.gitignore`: `.env`, `*.env`, `!.env.example`, `.cache/`, `memory/chroma/`, `memory/cache/`, `logs/`, `models/*.onnx`, `models/*.bin`, `whatsapp-bridge/auth/`.
2. **Rotate every key in `.env`** — assume the current ones are compromised the moment they touch a repo, even uncommitted. Cheap insurance.
3. `git add -A && git commit -m "Baseline: TRON-X at 7/16 advanced phases"`.
4. Tag it `v1.0.0-baseline`.

### 2.3 Residual code issues (low, real)

- **`executor.py:307`** — `execute_python_safe`'s outer `TimeoutError` handler still returns without `_kill_proc()`, unlike the other three runners which were fixed. (Its inner `_run` does kill, so impact is bounded, but the outer path is inconsistent.) Add `await _kill_proc(locals().get("proc"))`.
- **`chroma_db.py:197`** — MMR computes `query_vec` then never uses it; a wasted embedding call on every rerank. Delete the line.
- **`api/voice.py:387,400`** — `tts_done = asyncio.Event()` is created but never set/awaited; the streaming-TTS completion signal is dead. This is actually a *feature gap* — see Phase 26a in §4.
- **~50 unused imports** (pyflakes). Run `ruff --select F401 --fix`. Watch the ones inside `try:` import-guards (`cadquery`, `openwakeword`) — those are intentional.

### 2.4 Reproducibility

- `requirements.txt` is pinned but there's no lockfile and no hash pinning. Move to `uv` (already used by the bundled `kokoro-onnx`) or `pip-tools` to produce a hashed `requirements.lock`.
- `cadquery` is dead on Python 3.13. Don't downgrade the project — see the CAD sidecar in §4.3.

---

## 3. The structural 10× — enforce 5 layers + an event bus

The folders are clean but nothing stops `api/` from reaching into `system/` or an agent from importing a router internal. As the project grows past 100 files, "add a feature" increasingly means "edit it in six places." Fix the *shape*, not the contents.

**Target layering (dependencies point downward only):**

```
L5  api/            ── HTTP/WS surface, zero business logic
L4  orchestration/  ── orchestrator, coordinator, supervisor, scheduler
L3  capabilities/   ── agents, voice, vision, iot, feeds, system  (the "skills")
L2  services/       ── memory, router, analytics, plugins         (shared engines)
L1  core/           ── config, logger, auth, ratelimit, model_cache, event_bus
```

Two concrete mechanisms make this real and unlock the 2050 features:

**(a) A typed internal event bus** (`core/event_bus.py`). Today agents call each other directly and the coordinator stitches results. Replace point-to-point with publish/subscribe over Pydantic event models (`UserUtterance`, `IntentClassified`, `AgentResult`, `MemoryWritten`, `ProactiveTrigger`, `DeviceStateChanged`). This is what makes proactivity, the live activity feed (SSE), and any future "watcher" agent *trivial to add* — they just subscribe. It also gives you a single chokepoint to record a full causal trace of every interaction (priceless for the eval harness and debugging).

**(b) A capability manifest.** Each capability in L3 declares, in a small descriptor, the intents it serves, the events it emits/consumes, its cost profile, and its safety class. The orchestrator routes off the manifest instead of hardcoded `if intent == ...` chains. Adding a capability becomes "drop a folder + descriptor," not "edit the orchestrator, the router, the API, and the intent map."

**Migration is mechanical and safe** because the test-per-phase discipline already exists: move one slice at a time behind its existing tests, commit per slice. Estimate ~2–3 focused days; no behavior change, pure structure. Do it *after* §2 and *before* the big features.

---

## 4. The 2050 vision — grounded moonshots

Theme: **TRON-X stops being a thing you talk to and becomes a presence that thinks alongside you.** Every item below has a real implementation path on the existing stack. Items map onto the already-planned-but-pending phases where they exist, and extend well past them.

### 4.1 The Anticipation Engine — *the flagship*
**What it feels like:** You sit down at 8:50am and the HUD already shows: today's calendar with a flagged conflict, the two emails that actually need you, your commute weather, and "the deploy you started Friday is still red." You didn't ask. It inferred.

**How it's built on what exists:** calendar + email + reminders + feeds + scheduler + episodic memory are *all already here* — they just only respond. Add a single `orchestration/anticipator.py` that:
- subscribes to the event bus and to a nightly **memory-consolidation job** (run `period_summary()`, promote recurring episode topics into the `knowledge` collection, prune with the now-working `forget_before()`),
- learns your daily rhythm from episodic timestamps (Phase 31 — Adaptive Persona/Preference Learning, pending),
- emits `ProactiveTrigger` events that the HUD renders as cards and TTS can speak on wake.

This is the highest wow-to-effort ratio in the whole plan. Build it first among the features.

### 4.2 Ambient, always-on, full-duplex voice — *true Jarvis*
`wake_word.py` exists but isn't in a continuous loop, and streaming TTS is half-wired (`tts_done` dead, §2.3). Finish the loop:
- **Phase 26a** — Web Audio streaming TTS playback (fix the dead event, stream chunks).
- **Phase 26b** — VAD-based barge-in (`vad.py` exists): you can interrupt mid-sentence and it stops and listens. This is the single biggest "feels like the future" upgrade to the voice experience.
- **Phase 25** — speaker biometrics: voice-keyed authorization so "transfer money / unlock the door" verifies *who* is speaking, not just *what* was said.

### 4.3 Self-extending capabilities — *the assistant writes its own tools*
You already have an **ephemeral Docker code sandbox** (Phase 27, in progress) and a plugin registry. Combine them: when TRON-X hits a task it has no capability for, it can *draft* a new capability (a plugin conforming to the §3 manifest), test it in the sandbox, and — gated behind your explicit approval — register it live. This is auditable, reversible (it's just a plugin), and safe (sandboxed + approval-gated). It's the grounded version of "self-coding AI": bounded, observed, human-in-the-loop.
- Pair with the **CAD sidecar**: a Python-3.11 container exposing CadQuery over HTTP, resolving the 3.13 incompatibility without touching the main app.

### 4.4 A living knowledge graph — *memory you can walk through*
Promote episodic memory from a search box to a navigable map. Extract an entity-relationship graph (people, projects, devices, recurring topics) from episodes during the nightly consolidation, store edges in Supabase, and render it in the HUD as an interactive graph. "What do you know about X" returns a subgraph, not a list. This is the substrate that makes the Anticipation Engine *explainable* — every proactive nudge can point at the memory that triggered it.

### 4.5 The autonomous home — *rules that you speak into existence*
`ws_listener.py` already streams Home Assistant events; `nl_mapper.py` translates NL→commands. The missing 20% is **Phase 35's rules engine**: condition→action automations authored by voice ("if motion in the hall after 11pm, dim the lights to 10%"; "when I say goodnight, run the goodnight scene"). Add anomaly detection on the event stream (door open at 3am → proactive alert via the same `ProactiveTrigger` path as §4.1).

### 4.6 Self-tuning intelligence — *it gets better while you sleep*
- **Phase 36** — self-tuning router: feed the A/B outcomes and the (currently discarded) LiteLLM per-call cost/latency back into router weights so the model chain optimizes itself against quality-per-dollar over time.
- **Nightly eval harness**: a fixed canned-prompt set run against the live model chain to catch silent provider regressions/outages *before* you hit them. Wire results into the existing cost dashboard (Phase 34, done).
- This closes the loop: the system measures itself, and adjusts itself.

### 4.7 Presence everywhere — *reach you, reliably*
- **Telegram bridge** (Bot API, no browser, no QR re-auth) as a robust second channel — WhatsApp-Web automation is the most fragile component in the stack.
- **Push notifications** (ntfy.sh / Pushover) so proactive alerts reach your phone when you're away from the PC.
- **Phase 30** — distributed memory mesh + secured mobile API tunnel, so the assistant is the same brain across devices.
- Make the HUD an installable **PWA**.

### 4.8 Multimodal awareness — *it sees what you see*
Extend `vision/screen.py` with an opt-in webcam path: "what am I looking at," "read this document on my desk." Combined with the knowledge graph, visual context becomes memory ("you were holding the router manual at 4pm").

---

## 5. Sequenced roadmap

**Wave 0 — Stop the bleeding (½ day, do today)**
1. Fix HF cache (§2.1) + warm-models script + test.
2. `.gitignore` `.env`, rotate keys, first commit, tag baseline (§2.2).
3. Clear residual code issues (§2.3): `executor.py:307`, MMR dead embed, `ruff --fix`.

**Wave 1 — Foundation (3–4 days)**
4. pytest smoke layer for `core/` + every `api/` router (TestClient, mocked LLM) + CI (ruff + py_compile + pytest).
5. Lockfile via `uv`/`pip-tools`.
6. Structural refactor: 5 layers + event bus + capability manifest (§3), one slice per commit behind existing tests.

**Wave 2 — The proactivity threshold (1 week)**  ← biggest payoff
7. Memory consolidation nightly job + knowledge graph extraction (§4.4).
8. **Anticipation Engine** + morning briefing on the event bus (§4.1).
9. Live agent-activity SSE feed in the HUD (falls out of the event bus for free).

**Wave 3 — Ambient & autonomous (1–2 weeks)**
10. Voice: 26a streaming TTS → 26b barge-in → 25 speaker biometrics (§4.2).
11. Rules engine + home anomaly alerts, Phase 35 (§4.5).
12. Self-tuning router + nightly eval harness, Phase 36 (§4.6).

**Wave 4 — Frontier (ongoing)**
13. Self-extending capabilities via sandbox+plugins + CAD sidecar (§4.3).
14. Telegram bridge + push + PWA + memory mesh (§4.7).
15. Webcam multimodal awareness (§4.8).

---

## 6. Open decisions for you

1. **HF cache target** — adopt the portable `<project>/.cache/huggingface` default, or keep an explicit `hf_cache_dir` pointing at your existing populated `hub` on `D:` (set once in `.env`, no longer in source)? I recommend the latter for you, the former for any other machine — the new code supports both with the *same* code path.
2. **Key rotation** — are you willing to rotate the keys in `.env` before the first commit? (Strongly recommended; the alternative is keeping the repo permanently `.env`-free and never trusting history.)
3. **Refactor appetite** — do the §3 layering now (clean foundation, ~3 days, no new features) or defer it and build the Anticipation Engine first on the current structure (faster wow, more debt)? I recommend the refactor first; it makes everything after it cheaper.
4. **Flagship pick** — if you want one feature built end-to-end next, my vote is the Anticipation Engine (§4.1): every dependency already exists and it changes the entire feel of the product.

---

*This plan deliberately does not duplicate `TRONX_EVOLUTION_ROADMAP.md` (phases 21–36) — it builds on it, marks what's already shipped, and reframes the remaining phases around the proactivity thesis. The bug list in `CODE_REVIEW_logical_errors.md` was verified against live source and is now largely stale; §2 supersedes it.*
