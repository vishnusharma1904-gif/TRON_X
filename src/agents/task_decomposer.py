"""
TRON-X Multi-Agent Task Decomposer
────────────────────────────────────
Breaks complex user goals into an ordered plan of sub-tasks,
routes each sub-task to the appropriate specialist agent,
then synthesises the results into a final coherent response.

Architecture:
  Planner LLM → [subtask list] → Agent Router → [parallel/sequential execution]
                                              → Result Synthesiser
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from src.core.logger import log


# ── Sub-task types & routing map ───────────────────────────────────────────────

AGENT_TYPES = {
    "research":  "ResearchAgent",
    "code":      "CodeAgent",
    "system":    "SystemAgent",
    "iot":       "IoTAgent",
    "memory":    "MemoryAgent",
    "vision":    "VisionAgent",
    "chat":      "ChatAgent",
    "cad":       "CADAgent",
}


# ── Planner ────────────────────────────────────────────────────────────────────

_PLANNER_PROMPT = """\
You are a task decomposition engine.
Break the user's goal into an ordered list of sub-tasks.
Each sub-task must have:
  - "id": short snake_case identifier
  - "type": one of [research, code, system, iot, memory, vision, chat, cad]
  - "description": exactly what to do
  - "depends_on": list of ids this task depends on (empty for first tasks)
  - "parallel": true if this can run in parallel with sibling tasks

Return ONLY a JSON array. No markdown, no explanation.
Example:
[
  {"id":"search","type":"research","description":"Search for X","depends_on":[],"parallel":false},
  {"id":"summarise","type":"chat","description":"Summarise findings","depends_on":["search"],"parallel":false}
]
"""


async def plan_tasks(goal: str, persona: str = "jarvis") -> list[dict]:
    """Ask the LLM to decompose a goal into sub-tasks."""
    from src.intelligence.orchestrator import get_orchestrator
    orch = get_orchestrator()

    result = await orch.chat(
        user_message=f"Goal: {goal}",
        session_id="__planner__",
        intent="reasoning",
        persona=persona,
        max_tokens=800,
        temperature=0.2,
        extra_system=_PLANNER_PROMPT,
    )
    reply = result.get("reply", "[]")

    try:
        # Strip code fences
        clean = re.sub(r"```(?:json)?", "", reply).strip()
        tasks = json.loads(clean)
        log.info(f"[planner] Decomposed into {len(tasks)} sub-tasks")
        return tasks
    except Exception as e:
        log.warning(f"[planner] JSON parse failed ({e}), falling back to single task")
        return [{"id": "main", "type": "chat", "description": goal,
                 "depends_on": [], "parallel": False}]


# ── Plan reviser (Phase 21) ─────────────────────────────────────────────────────

_REVISE_PROMPT = """\
You are a task supervisor reviewing progress on a multi-step plan.
Given the original goal, the full plan, the steps completed so far (with
their results), and the most recent step's result, decide whether to:
  - "continue": the plan is still on track, no changes needed
  - "done": the goal is already fully achieved, stop early
  - "replace_remaining": the remaining steps need to change (e.g. a step
    failed and needs a different approach, or new information changes what's
    needed)

Return ONLY a JSON object with this exact shape. No markdown, no explanation.
{"action": "continue"|"done"|"replace_remaining", "new_plan": [...], "reason": "short explanation"}

"new_plan" is only used when action is "replace_remaining", and must be a
list of sub-tasks following the same schema as the original plan (id, type,
description, depends_on, parallel) -- these replace ALL remaining
(not-yet-executed) steps. For "continue" or "done", "new_plan" should be [].
"""


def _extract_json_object(text: str) -> Optional[str]:
    """Extract the first balanced {...} JSON object substring from text."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _parse_revision(reply: str) -> dict:
    """
    Robustly parse revise_plan()'s JSON response. Handles markdown code
    fences, trailing commas, extra prose around the JSON object, and
    completely unparseable replies (defaults to "continue" in all of those
    cases so a re-planning hiccup never blocks execution).
    """
    clean = re.sub(r"```(?:json)?", "", reply or "").strip()

    candidates = [clean]
    extracted = _extract_json_object(clean)
    if extracted and extracted != clean:
        candidates.append(extracted)

    for candidate in candidates:
        for text in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                data = json.loads(text)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("action") in (
                "continue", "done", "replace_remaining"
            ):
                data.setdefault("new_plan", [])
                data.setdefault("reason", "")
                return data

    log.warning(f"[supervisor] revise_plan: unparseable response, defaulting to 'continue': {reply[:200]!r}")
    return {"action": "continue", "new_plan": [], "reason": "unparseable revise_plan response"}


async def revise_plan(
    goal: str,
    plan: list[dict],
    completed: list[dict],
    last_result: dict,
    persona: str = "jarvis",
    session_id: str = "__planner__",
) -> dict:
    """
    Ask the LLM whether the plan needs revising given progress so far.
    Always returns a dict with at least {"action", "new_plan", "reason"}.
    Never raises -- LLM/parse failures degrade to {"action": "continue"}.
    """
    from src.intelligence.orchestrator import get_orchestrator

    completed_summary = "\n".join(
        f"[{r.get('id')}] success={r.get('success')}: "
        f"{str(r.get('result', r.get('error', '')))[:300]}"
        for r in completed
    )
    prompt = (
        f"Goal: {goal}\n\n"
        f"Original/current plan: {json.dumps(plan)}\n\n"
        f"Completed so far:\n{completed_summary}\n\n"
        f"Most recent step result: {json.dumps(last_result)[:8192]}\n"
    )

    try:
        orch = get_orchestrator()
        result = await orch.chat(
            user_message=prompt,
            session_id=session_id,
            intent="reasoning",
            persona=persona,
            temperature=0.2,
            extra_system=_REVISE_PROMPT,
        )
        reply = result.get("reply", "")
    except Exception as e:
        log.warning(f"[supervisor] revise_plan LLM call failed ({e}), continuing with current plan")
        return {"action": "continue", "new_plan": [], "reason": f"revise_plan error: {e}"}

    return _parse_revision(reply)


# ── Agent executor ─────────────────────────────────────────────────────────────

async def _execute_subtask(
    task: dict,
    context: dict[str, Any],
    persona: str,
    session_id: str,
) -> dict:
    """Execute a single sub-task and return its result."""
    t_id   = task["id"]
    t_type = task.get("type", "chat")
    desc   = task["description"]

    # Inject results from dependency tasks into description
    deps = task.get("depends_on", [])
    dep_context = ""
    for dep in deps:
        if dep in context:
            dep_context += f"\n[Result of '{dep}']: {context[dep]}\n"

    full_desc = desc + dep_context if dep_context else desc
    log.info(f"[agent] Executing '{t_id}' ({t_type}): {desc[:60]}…")

    try:
        if t_type == "research":
            from src.agents.research_agent import ResearchAgent
            result = await ResearchAgent().run(full_desc, persona=persona)

        elif t_type == "code":
            from src.agents.code_agent import CodeAgent
            result = await CodeAgent().run(full_desc, persona=persona)

        elif t_type == "cad":
            from src.agents.cad_agent import CADAgent
            result = await CADAgent().run(full_desc, persona=persona)

        elif t_type == "system":
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            res = await orch.chat(full_desc, session_id, "system", persona)
            result = res.get("reply", "")

        elif t_type == "memory":
            from src.memory.rag import get_rag
            rag = get_rag()
            ctx, hits = await rag.retrieve(full_desc, top_k=5)
            result = ctx or "No relevant memories found."

        elif t_type == "vision":
            from src.agents.vision_agent import VisionAgent
            # full_desc may contain a file path after "file:" prefix
            import re as _re
            _m = _re.search(r'file:(\S+)', full_desc)
            img_path = _m.group(1) if _m else None
            result = await VisionAgent().run(
                full_desc, image_path=img_path, persona=persona, session_id=session_id
            )

        else:  # chat / iot / fallback
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            res = await orch.chat(full_desc, session_id, t_type, persona)
            result = res.get("reply", "")

        context[t_id] = result
        return {"id": t_id, "type": t_type, "success": True, "result": result}

    except Exception as e:
        log.error(f"[agent] Sub-task '{t_id}' failed: {e}")
        context[t_id] = f"[ERROR: {e}]"
        return {"id": t_id, "type": t_type, "success": False, "error": str(e)}


# ── Orchestrator ───────────────────────────────────────────────────────────────

async def _synthesise(goal: str, results: list[dict], persona: str, session_id: str) -> str:
    """Ask LLM to combine all sub-task results into a final answer."""
    from src.intelligence.orchestrator import get_orchestrator
    orch = get_orchestrator()

    summary = "\n".join(
        f"[{r['id']}] ({r['type']}): {r.get('result', r.get('error', ''))[:16384]}"
        for r in results
    )
    prompt = (
        f"Original goal: {goal}\n\n"
        f"Sub-task results:\n{summary}\n\n"
        "Synthesise a complete, final answer from the above results.\n"
        "If the user requested a report, you MUST output a fully functional, complete, neatly formatted Python script that generates the report. Do NOT output a summary, do NOT truncate the code, and do NOT use placeholders. Output the FULL script."
    )
    res = await orch.chat(prompt, session_id, "chat", persona)
    return res.get("reply", summary)


async def run_agent_pipeline(
    goal: str,
    persona: str = "jarvis",
    session_id: str = "__agents__",
    max_parallel: int = 4,
) -> dict:
    """
    Full multi-agent pipeline:
      1. Decompose goal into sub-tasks
      2. Execute in dependency order (parallel where allowed)
      3. Synthesise final answer
    """
    # 1. Plan
    tasks = await plan_tasks(goal, persona)
    if not tasks:
        return {"goal": goal, "reply": "Could not decompose task.", "steps": []}

    # 2. Execute in topological order
    context: dict[str, Any] = {}
    completed: set[str]     = set()
    all_results: list[dict] = []

    remaining = list(tasks)
    while remaining:
        # Find tasks whose dependencies are all met
        ready = [t for t in remaining if set(t.get("depends_on", [])).issubset(completed)]
        if not ready:
            # Circular dependency or stall — run first remaining
            ready = [remaining[0]]

        # Split into parallel and sequential groups
        parallel_group = [t for t in ready if t.get("parallel")]
        sequential     = [t for t in ready if not t.get("parallel")]

        tasks_to_run = []
        if parallel_group:
            tasks_to_run.extend(parallel_group[:max_parallel])
        elif sequential:
            tasks_to_run.append(sequential[0])

        # Execute batch
        batch_results = await asyncio.gather(*[
            _execute_subtask(t, context, persona, session_id)
            for t in tasks_to_run
        ])
        all_results.extend(batch_results)
        for r in batch_results:
            completed.add(r["id"])
        for t in tasks_to_run:
            remaining.remove(t)

    # 3. Synthesise

    final_reply = await _synthesise(goal, all_results, persona, session_id)

    return {
        "goal": goal,
        "reply": final_reply,
        "steps": all_results,
        "total_steps": len(all_results),
        "completed": len([r for r in all_results if r.get("success")]),
        "failed": len([r for r in all_results if not r.get("success")]),
    }

async def stream_agent_pipeline(
    goal: str,
    persona: str = "jarvis",
    session_id: str = "__agents__",
    max_parallel: int = 4,
):
    import json
    import asyncio
    def _evt(d: dict) -> str:
        return f"data: {json.dumps(d)}\n\n"

    tasks = await plan_tasks(goal, persona)
    if not tasks:
        yield _evt({"type": "error", "error": "Could not decompose task."})
        return

    yield _evt({"type": "plan", "tasks": tasks})

    context: dict[str, Any] = {}
    completed: set[str]     = set()
    all_results: list[dict] = []
    remaining = list(tasks)

    while remaining:
        ready = [t for t in remaining if set(t.get("depends_on", [])).issubset(completed)]
        if not ready:
            ready = [remaining[0]]

        parallel_group = [t for t in ready if t.get("parallel")]
        sequential     = [t for t in ready if not t.get("parallel")]

        tasks_to_run = []
        if parallel_group:
            tasks_to_run.extend(parallel_group[:max_parallel])
        elif sequential:
            tasks_to_run.append(sequential[0])

        for t in tasks_to_run:
            yield _evt({"type": "task_start", "task": t})

        pending = {
            asyncio.create_task(_execute_subtask(t, context, persona, session_id)): t
            for t in tasks_to_run
        }
        
        while pending:
            done, _ = await asyncio.wait(list(pending.keys()), return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                t = pending.pop(fut)
                res = fut.result()
                all_results.append(res)
                completed.add(res["id"])
                yield _evt({"type": "task_result", "task": res})

        for t in tasks_to_run:
            remaining.remove(t)

    yield _evt({"type": "synthesis_start"})
    from src.intelligence.orchestrator import get_orchestrator
    orch = get_orchestrator()
    summary = "\n".join(
        f"[{r['id']}] ({r['type']}): {r.get('result', r.get('error', ''))[:16384]}"
        for r in all_results
    )
    prompt = (
        f"Original goal: {goal}\n\n"
        f"Sub-task results:\n{summary}\n\n"
        "Synthesise a complete, final answer from the above results.\n"
        "If the user requested a report, you MUST output a fully functional, complete, neatly formatted Python script that generates the report. Do NOT output a summary, do NOT truncate the code, and do NOT use placeholders. Output the FULL script."
    )
    
    reply = ""
    try:
        async for chunk in orch.chat_stream(prompt, session_id, "chat", persona):
            content = chunk.get("content", "")
            if content:
                reply += content
                yield _evt({"type": "synthesis_chunk", "chunk": content})
    except Exception as e:
        from src.core.logger import log
        log.warning(f"[agent_pipeline] Synthesis stream failed: {e}")
        res = await orch.chat(prompt, session_id, "chat", persona)
        reply = res.get("reply", summary)
        yield _evt({"type": "synthesis_chunk", "chunk": reply})

    yield _evt({"type": "synthesis_done", "reply": reply})
