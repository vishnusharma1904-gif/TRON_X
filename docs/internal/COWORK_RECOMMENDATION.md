# TRON-X Cowork — Architecture Recommendation

*Phase 38 companion doc · June 2026*

## The question

"Create a separate co-work kind of software" could mean three things: a new web
UI workspace on top of the existing API, a new backend orchestration module, or
a fully standalone repo/application. This doc recommends one and explains why.

## Recommendation: a separate front-end workspace, same backend

Build Cowork as a **distinct web workspace served by the existing TRON-X
server** (shipped in this phase as `static/cowork.html`, reachable at
`/static/cowork.html`), and *not* as a separate repo or a second backend.

The reasoning is mostly about what TRON-X already is. The backend already
contains every primitive a cowork product needs: LLM-planned task decomposition
(`task_decomposer.py`), supervised execution with re-planning (`supervisor.py`,
and now `parallel_supervisor.py` for true concurrency), a registry-based
dispatcher with SSE streaming (`coordinator.py`), deliberative reasoning
(`intelligence/reasoning.py`), universal attachment ingestion
(`ingestion/attachments.py`), memory/RAG, analytics, auth, and rate limiting. A
second backend would duplicate the router, auth, provider failover, and memory
layers — the exact six-places-per-feature problem the 10X plan warns about.

A fully standalone repo has the same duplication problem plus operational drag
(two deploys, two .envs, CORS, version skew between API and client). It only
becomes the right call if Cowork ever needs to be sold/shipped separately from
TRON-X. That bridge can be crossed later: because Cowork is a single static
file speaking only public HTTP APIs, extracting it to its own repo later is a
copy-paste, not a rewrite.

## What was shipped now

`static/cowork.html` — a zero-build, single-file workspace ("mission control")
that exercises the Phase 38 capabilities end to end: a mission input with three
modes (Parallel multi-agent, Sequential supervised, Deliberative reasoning),
drag-and-drop attachments of any supported type routed through
`/api/attachments/chat`, a live crew board showing per-step results,
re-plan/concurrency badges, and a confidence meter for reasoning runs. It
respects the optional `X-API-Key` auth header.

## Growth path (when you want more)

1. **Live streaming board** — switch `/api/agents/run` usage to an SSE variant
   (the pattern already exists in `/api/agents/coordinate/stream`), so steps
   appear as they execute rather than on completion.
2. **Shared sessions** — persist cowork missions in the existing session store
   so a mission can be reopened, re-run, or handed to the scheduler
   (`/api/agents/schedule`) as a recurring job.
3. **Multi-user** — the auth layer already supports multiple API keys; add a
   `user` tag to analytics events to get per-person mission history.
4. **Extract to its own repo** — only if Cowork needs independent distribution.
   The seam is already clean: one HTML file + the public API.
