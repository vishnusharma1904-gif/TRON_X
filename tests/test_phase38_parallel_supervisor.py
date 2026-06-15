"""
Phase 38 — Parallel Agentic Supervisor
Standalone test (conventions per TRONX_PHASE21-36_PROGRESS_HANDOFF.md §3).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if "chromadb" not in sys.modules:
    sys.modules["chromadb"] = MagicMock()
    sys.modules["chromadb.config"] = MagicMock()

from src.agents import parallel_supervisor as psup  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def step(id_: str, deps: list[str] | None = None, parallel: bool = False) -> dict:
    return {"id": id_, "type": "chat", "description": f"do {id_}",
            "depends_on": deps or [], "parallel": parallel}


# =============================================================================
print("\n=== _select_frontier ===")
# =============================================================================

sel = psup.ParallelSupervisorAgent._select_frontier

batch = sel([step("a", parallel=True), step("b", parallel=True), step("c")], set(), 4)
check("all parallel-ready picked", [t["id"] for t in batch] == ["a", "b"], batch)

batch = sel([step("a"), step("b")], set(), 4)
check("sequential -> single task", [t["id"] for t in batch] == ["a"], batch)

batch = sel([step("b", deps=["a"])], set(), 4)
check("stalled frontier forces first", [t["id"] for t in batch] == ["b"], batch)

batch = sel([step(f"t{i}", parallel=True) for i in range(6)], set(), 3)
check("max_parallel cap respected", len(batch) == 3, batch)

batch = sel([step("b", deps=["a"], parallel=True), step("c")], {"a"}, 4)
check("dep-satisfied parallel task picked", [t["id"] for t in batch] == ["b"], batch)


# =============================================================================
print("\n=== run(): true concurrency for parallel batch ===")
# =============================================================================

async def _concurrency_test():
    plan = [step("p1", parallel=True), step("p2", parallel=True),
            step("p3", parallel=True)]
    running = {"now": 0, "peak": 0}

    async def fake_execute(task, context, persona, session_id):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        context[task["id"]] = "ok"
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    t0 = time.monotonic()
    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(psup, "_execute_subtask", new=fake_execute), \
         patch.object(psup, "_synthesise", new=AsyncMock(return_value="final")), \
         patch.object(psup, "revise_plan", new=AsyncMock(
             return_value={"action": "continue", "new_plan": [], "reason": ""})):
        out = await psup.ParallelSupervisorAgent(max_parallel=4).run("goal")
    elapsed = time.monotonic() - t0

    check("all 3 steps completed", out["total_steps"] == 3, out)
    check("peak concurrency == 3", running["peak"] == 3, running)
    check("wall time ~1 batch not 3 (<0.12s)", elapsed < 0.12, f"{elapsed:.3f}s")
    check("max_concurrency reported", out["max_concurrency"] == 3, out)
    check("single tick", out["ticks"] == 1, out)

asyncio.run(_concurrency_test())


# =============================================================================
print("\n=== run(): dependency ordering respected ===")
# =============================================================================

async def _dependency_test():
    plan = [step("a", parallel=True), step("b", parallel=True),
            step("c", deps=["a", "b"])]
    order: list[str] = []

    async def fake_execute(task, context, persona, session_id):
        order.append(task["id"])
        context[task["id"]] = "ok"
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(psup, "_execute_subtask", new=fake_execute), \
         patch.object(psup, "_synthesise", new=AsyncMock(return_value="final")), \
         patch.object(psup, "revise_plan", new=AsyncMock(
             return_value={"action": "continue", "new_plan": [], "reason": ""})):
        out = await psup.ParallelSupervisorAgent().run("goal")

    check("c runs last (after a,b)", order.index("c") == 2, order)
    check("two ticks (batch then c)", out["ticks"] == 2, out)

asyncio.run(_dependency_test())


# =============================================================================
print("\n=== run(): 'done' stops early ===")
# =============================================================================

async def _done_test():
    plan = [step("s1"), step("s2"), step("s3")]

    async def fake_execute(task, context, persona, session_id):
        context[task["id"]] = "ok"
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    async def fake_revise(goal, plan_, completed, last_result, persona="jarvis",
                          session_id="__planner__"):
        return {"action": "done", "new_plan": [], "reason": "already satisfied"}

    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(psup, "_execute_subtask", new=fake_execute), \
         patch.object(psup, "_synthesise", new=AsyncMock(return_value="final")), \
         patch.object(psup, "revise_plan", new=fake_revise), \
         patch.object(psup, "_log_revision"):
        out = await psup.ParallelSupervisorAgent().run("goal")

    check("stopped after first step", out["total_steps"] == 1, out)
    check("one revision recorded", out["revisions"] == 1, out)

asyncio.run(_done_test())


# =============================================================================
print("\n=== run(): 'replace_remaining' swaps the tail ===")
# =============================================================================

async def _replace_test():
    plan = [step("s1"), step("old2"), step("old3")]
    executed: list[str] = []
    fired = {"done": False}

    async def fake_execute(task, context, persona, session_id):
        executed.append(task["id"])
        context[task["id"]] = "ok"
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    async def fake_revise(goal, plan_, completed, last_result, persona="jarvis",
                          session_id="__planner__"):
        if not fired["done"]:
            fired["done"] = True
            return {"action": "replace_remaining",
                    "new_plan": [step("new2"), step("new3")], "reason": "pivot"}
        return {"action": "continue", "new_plan": [], "reason": ""}

    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(psup, "_execute_subtask", new=fake_execute), \
         patch.object(psup, "_synthesise", new=AsyncMock(return_value="final")), \
         patch.object(psup, "revise_plan", new=fake_revise), \
         patch.object(psup, "_log_revision"):
        out = await psup.ParallelSupervisorAgent().run("goal")

    check("old tail never ran", "old2" not in executed and "old3" not in executed, executed)
    check("new tail ran", executed == ["s1", "new2", "new3"], executed)
    check("one revision", out["revisions"] == 1, out)

asyncio.run(_replace_test())


# =============================================================================
print("\n=== run(): revision cap honoured ===")
# =============================================================================

async def _cap_test():
    plan = [step(f"s{i}") for i in range(5)]
    revise_calls = {"n": 0}

    async def fake_execute(task, context, persona, session_id):
        context[task["id"]] = "ok"
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    async def fake_revise(goal, plan_, completed, last_result, persona="jarvis",
                          session_id="__planner__"):
        revise_calls["n"] += 1
        # Always tries to replace with the same remaining tail (forces revisions)
        remaining_ids = [t["id"] for t in plan_ if t["id"] not in
                         {c.get("id") for c in completed}]
        return {"action": "replace_remaining",
                "new_plan": [step(i) for i in remaining_ids], "reason": "churn"}

    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(psup, "_execute_subtask", new=fake_execute), \
         patch.object(psup, "_synthesise", new=AsyncMock(return_value="final")), \
         patch.object(psup, "revise_plan", new=fake_revise), \
         patch.object(psup, "_log_revision"):
        out = await psup.ParallelSupervisorAgent(max_revisions=2).run("goal")

    check("revisions capped at 2", out["revisions"] == 2, out)
    check("capped flag set", out["capped"] is True, out)
    check("cap note in reply", "capped" in out["reply"], out["reply"][-120:])
    check("revise not called after cap", revise_calls["n"] == 2, revise_calls)

asyncio.run(_cap_test())


# =============================================================================
print("\n=== run(): revise_plan failure degrades to continue ===")
# =============================================================================

async def _revise_fail_test():
    plan = [step("s1"), step("s2")]

    async def fake_execute(task, context, persona, session_id):
        context[task["id"]] = "ok"
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(psup, "_execute_subtask", new=fake_execute), \
         patch.object(psup, "_synthesise", new=AsyncMock(return_value="final")), \
         patch.object(psup, "revise_plan", new=AsyncMock(side_effect=RuntimeError("boom"))):
        out = await psup.ParallelSupervisorAgent().run("goal")

    check("all steps still completed", out["total_steps"] == 2, out)
    check("no revisions counted", out["revisions"] == 0, out)

asyncio.run(_revise_fail_test())


# =============================================================================
print("\n=== run(): empty plan handled ===")
# =============================================================================

async def _empty_plan_test():
    with patch.object(psup, "plan_tasks", new=AsyncMock(return_value=[])):
        out = await psup.ParallelSupervisorAgent().run("goal")
    check("graceful empty-plan result", out["results"] == [] and "Could not" in out["reply"], out)

asyncio.run(_empty_plan_test())


# =============================================================================
print(f"\n{'=' * 60}\nRESULT: {PASS} passed, {FAIL} failed\n{'=' * 60}")
sys.exit(1 if FAIL else 0)
