"""
TRON-X Parallel Agentic Supervisor  (Phase 38)
────────────────────────────────────────────────
An upgrade over `supervisor.py` (Phase 21). The original SupervisorAgent runs
one sub-task at a time (`ready[0]`), even when several sub-tasks have all their
dependencies satisfied and are marked `parallel`. That serialises work that
could overlap — the multi-tasking the user actually wants.

`ParallelSupervisorAgent` keeps the same re-planning feedback loop but executes
the *entire ready frontier* each tick:

  tick:
    1. Compute `ready` = tasks whose depends_on ⊆ completed.
    2. Partition ready into a parallel batch (capped at `max_parallel`) and,
       if none are parallel-eligible, fall back to a single sequential task.
    3. asyncio.gather the batch — true concurrency.
    4. After the batch, run revise_plan() ONCE on the aggregate progress and
       apply continue / done / replace_remaining (same semantics as Phase 21).
    5. Repeat until the plan is exhausted, capped, or 'done'.

Guarantees / safety:
  • Dependency order is always respected — a task never starts before its
    depends_on are in `completed`.
  • A stalled frontier (circular deps / unsatisfiable) is broken by forcing the
    first remaining task, so the loop always terminates.
  • Re-planning is capped at `max_revisions`; every real revision is appended to
    the same `memory/cache/plan_revisions.jsonl` audit log as Phase 21.
  • `_execute_subtask`, `_synthesise`, `plan_tasks`, `revise_plan` are imported
    from `task_decomposer` so behaviour stays consistent and tests can patch
    them at module scope (mirrors `tests/test_phase21_supervisor.py`).

This is additive: nothing in supervisor.py changes, and the API exposes this as
an opt-in mode.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from src.agents.task_decomposer import (
    _execute_subtask,
    _synthesise,
    plan_tasks,
    revise_plan,
)
from src.core.logger import log

PLAN_REVISIONS_LOG = "memory/cache/plan_revisions.jsonl"
REVISE_TIMEOUT_SEC = 30


def _log_revision(session_id: str, goal: str, revision_n: int,
                  old_plan: list[dict], new_plan: list[dict], reason: str) -> None:
    """Append a JSON line recording a plan revision. Never raises."""
    try:
        os.makedirs(os.path.dirname(PLAN_REVISIONS_LOG), exist_ok=True)
        with open(PLAN_REVISIONS_LOG, "a") as f:
            f.write(json.dumps({
                "timestamp": time.time(),
                "session_id": session_id,
                "goal": goal,
                "revision_n": revision_n,
                "old_plan": old_plan,
                "new_plan": new_plan,
                "reason": reason,
                "mode": "parallel",
            }) + "\n")
    except Exception as e:
        log.warning(f"[parallel-supervisor] failed to log plan revision: {e}")


class ParallelSupervisorAgent:
    """
    Dependency-aware, frontier-parallel supervisor with dynamic re-planning.

    Parameters
    ----------
    max_revisions : int
        Cap on real plan revisions (continue is free).
    max_parallel : int
        Max sub-tasks executed concurrently in a single tick.
    """

    def __init__(self, max_revisions: int = 3, max_parallel: int = 4):
        self.max_revisions = max(0, int(max_revisions))
        self.max_parallel = max(1, int(max_parallel))

    @staticmethod
    def _select_frontier(remaining: list[dict], completed_ids: set[str],
                         max_parallel: int) -> list[dict]:
        """
        Pick the batch to run this tick.

          • ready = tasks whose depends_on are all satisfied.
          • If nothing is ready (stall / cycle), force the first remaining task
            so the loop can make progress and terminate.
          • Among ready tasks, run all `parallel: true` ones together (capped).
          • If none are parallel-eligible, run exactly one sequential task to
            preserve ordering semantics.
        """
        ready = [t for t in remaining if set(t.get("depends_on", [])).issubset(completed_ids)]
        if not ready:
            return [remaining[0]]

        parallel_group = [t for t in ready if t.get("parallel")]
        if parallel_group:
            return parallel_group[:max_parallel]
        return [ready[0]]

    async def run(
        self,
        goal: str,
        persona: str = "jarvis",
        session_id: str = "__agents__",
    ) -> dict:
        plan = await plan_tasks(goal, persona)
        if not plan:
            return {
                "goal": goal, "reply": "Could not decompose task.",
                "plan": [], "results": [], "revisions": 0, "capped": False,
                "max_concurrency": 0, "ticks": 0, "mode": "parallel",
            }

        context: dict[str, Any] = {}
        completed_ids: set[str] = set()
        all_results: list[dict] = []
        remaining = list(plan)
        revisions = 0
        capped = False
        ticks = 0
        max_concurrency = 0

        while remaining:
            ticks += 1
            batch = self._select_frontier(remaining, completed_ids, self.max_parallel)
            max_concurrency = max(max_concurrency, len(batch))

            # Execute the batch concurrently.
            batch_results = await asyncio.gather(*[
                _execute_subtask(t, context, persona, session_id) for t in batch
            ])
            all_results.extend(batch_results)
            for r in batch_results:
                completed_ids.add(r["id"])
            for t in batch:
                remaining.remove(t)

            if not remaining:
                break

            if revisions >= self.max_revisions:
                capped = True
                continue  # run out the remaining plan as-is

            # Re-plan once per tick, against the most recent batch result.
            last_result = batch_results[-1] if batch_results else {}
            try:
                revised = await asyncio.wait_for(
                    revise_plan(goal, plan, all_results, last_result, persona, session_id),
                    timeout=REVISE_TIMEOUT_SEC,
                )
            except Exception as e:
                log.warning(f"[parallel-supervisor] revise_plan failed/timed out ({e}); continuing")
                revised = {"action": "continue", "new_plan": [], "reason": f"revise error: {e}"}

            action = revised.get("action", "continue")
            if action == "done":
                revisions += 1
                _log_revision(session_id, goal, revisions, remaining, [], revised.get("reason", ""))
                remaining = []
                break
            elif action == "replace_remaining":
                new_tail = revised.get("new_plan") or []
                if new_tail:
                    revisions += 1
                    _log_revision(session_id, goal, revisions, remaining, new_tail,
                                  revised.get("reason", ""))
                    plan = [r for r in plan if r.get("id") in completed_ids] + new_tail
                    remaining = list(new_tail)
                # empty new_plan ⇒ treat as continue
            # action == "continue": no change

        final_reply = await _synthesise(goal, all_results, persona, session_id)
        if capped:
            final_reply += ("\n\n(Note: re-planning was capped after the maximum number of "
                            "revisions; remaining steps ran as planned.)")

        return {
            "goal": goal,
            "reply": final_reply,
            "final": final_reply,
            "plan": plan,
            "results": all_results,
            "total_steps": len(all_results),
            "completed": len([r for r in all_results if r.get("success")]),
            "failed": len([r for r in all_results if not r.get("success")]),
            "revisions": revisions,
            "capped": capped,
            "ticks": ticks,
            "max_concurrency": max_concurrency,
            "mode": "parallel",
        }
