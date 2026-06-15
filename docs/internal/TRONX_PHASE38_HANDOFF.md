# TRON-X Phase 38 Handoff — Advanced Reasoning, Parallel Agents, Universal Attachments, Cowork

*Session date: 2026-06-11 · Status: implemented & tested*

## What Phase 38 adds

Three capability pillars plus a workspace UI, all additive (no existing
behaviour changed by default):

### 1. Deliberative reasoning — `src/intelligence/reasoning.py`

`DeliberativeReasoner(samples, reflect, verify)` layers active compute on top
of the static CoT injection in `cot.py`:

- **Self-consistency**: N independent reasoning paths sampled concurrently at
  temperature, majority-voted (normalised buckets), with an LLM aggregation
  pass when the vote is split (<50% agreement).
- **Reflection**: one critique-and-revise pass on the chosen answer.
- **Verification**: an independent verifier returns
  `{verdict, confidence, issue}`; final confidence blends verifier confidence
  with sample agreement (fail verdicts suppress confidence).
- Every LLM call goes through `get_orchestrator().chat(...)` (lazy import,
  fully mockable). All failures degrade — worst case is one plain answer,
  never an exception. `samples=1, reflect=False, verify=False` = exactly 1
  LLM call.

API: `POST /api/agents/reason`
`{question, samples=3, reflect=true, verify=true, temperature=0.7, persona, session_id}`
→ `{answer, confidence, samples, agreement, votes, reflected, verified, trace}`.

### 2. Parallel agentic supervisor — `src/agents/parallel_supervisor.py`

`ParallelSupervisorAgent(max_revisions=3, max_parallel=4)` upgrades the Phase
21 supervisor from one-step-at-a-time to **frontier-parallel execution**: each
tick, all dependency-satisfied `parallel: true` sub-tasks run concurrently via
`asyncio.gather`, then `revise_plan()` runs once per tick with the same
continue/done/replace_remaining semantics (and the same
`memory/cache/plan_revisions.jsonl` audit log, tagged `"mode": "parallel"`).
Stalled frontiers (cycles) are broken by forcing the first remaining task.
Result dict adds `ticks`, `max_concurrency`, `mode`.

API: `POST /api/agents/run` gains `mode: "sequential"|"parallel"` and
`max_parallel` (only honoured when `supervised: true`; default behaviour
unchanged).

### 3. Universal attachments — `src/ingestion/attachments.py` (+ `src/api/attachments.py`)

One entry point normalising any upload into an `Attachment` (text and/or
multimodal `image_data` blocks in the exact shape `orchestrator.chat`
expects):

| kind     | formats | result |
|----------|---------|--------|
| document | pdf, docx, pptx, xlsx/xls | extracted text (+tables/slides/sheets) |
| data     | json, csv/tsv, yaml, xml, html, ini, toml | text (json pretty-printed, csv piped) |
| code     | ~40 extensions | verbatim text |
| image    | png/jpg/gif/webp/bmp/tiff (svg → source text) | `image_data` data-URL block |
| audio    | mp3/wav/m4a/… | flagged `needs_stt` (transcribes via voice module when `enable_stt=True`) |
| video    | mp4/mov/… | accepted with metadata note |
| archive  | zip | manifest + inlined small text files |
| unknown  | — | sniffed: text if printable, else marked binary |

Graceful degradation everywhere: a missing library yields
`extracted=False` + an install hint, never an exception. Text capped at
`max_chars` (24k default) with a `truncated` flag.

API (`/api/attachments`): `GET /supported`, `POST /process` (extract only),
`POST /chat` (multimodal chat with files as context), `POST /ingest` (store
into ChromaDB knowledge base via the existing `ingest_text`).
Limits: 10 files, 50 MB each.

`requirements.txt`: added `python-pptx==1.0.2`, `openpyxl==3.1.5`.

### 4. Cowork workspace — `static/cowork.html` (+ `docs/COWORK_RECOMMENDATION.md`)

Single-file workspace at `/static/cowork.html`: mission input, three modes
(Parallel / Sequential / Reason), drag-drop attachments routed through
`/api/attachments/chat`, live crew board with per-step results, re-plan and
concurrency badges, confidence meter. Architecture rationale (separate
front-end, same backend) in `docs/COWORK_RECOMMENDATION.md`.

## Critical repair made along the way

**`src/main.py` was corrupted on disk** — an older copy of the router section
had been spliced into the file (a shorter write over a longer file without
truncation), colliding mid-line at the old `root()` and leaving a syntax error
that prevented the app from booting. Repaired by removing the stale duplicated
block (the surviving block is the newer one, with Phase 37 proactive router and
Phase 17/20 middleware). The attachments router (Phase 38) is registered there.
⚠️ Recommendation repeated from the 10X plan: **make a git commit** — the repo
still has zero commits, and this corruption is exactly the failure mode commits
protect against.

## Tests (repo conventions: standalone, mock LLM, PASS/FAIL, exit code)

| suite | checks |
|-------|--------|
| `tests/test_phase38_reasoning.py` | 36/36 |
| `tests/test_phase38_parallel_supervisor.py` | 24/24 (incl. real-concurrency timing + dependency-order proofs) |
| `tests/test_phase38_attachments.py` | 46/46 (real PDF/DOCX/PPTX/XLSX/ZIP fixtures built in-memory) |

Regressions re-run, all green: Phase 21 (40/40), 22, 23 (30/30), 28 (74/74),
29 (56/56), 33 (47/47), 34. Phase 37's suite failed only in the *test sandbox*
because its synced `config.py` snapshot predated the proactive settings — the
real `src/core/config.py` has them (verified, line 73); re-run on the host to
confirm.

## Not done / next

- SSE streaming variant of the parallel supervisor (board updates live).
- Audio transcription wiring (`transcribe_bytes` adapter in `src/voice/stt.py`)
  and video keyframe/transcript extraction.
- `reason` as an orchestrator intent (auto-escalate hard questions to the
  DeliberativeReasoner based on intent + complexity).
- Git init + first commit. Seriously.
