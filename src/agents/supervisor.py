"""
TRON-X Stateful Supervisor (Phase 21)
──────────────────────────────────────
Wraps the LLM-planned multi-agent pipeline (`task_decomposer.py`) with a
feedback loop: after each sub-task executes, the plan is re-evaluated using
its actual result via `revise_plan()`. The supervisor can:
  - "continue"          — keep executing the (remaining) plan as-is
  - "done"              — stop early, the goal is already satisfied
  - "replace_remaining" — swap the not-yet-executed steps for a new plan

Re-planning is capped at `max_revisions` (default 3); once the cap is hit,
the remaining plan executes as originally planned (or last-replaced) and the
response notes that re-planning was capped.

Every actual revision (action != "continue") is appended to
memory/cache/plan_revisions.jsonl for audit/debugging.
"""
from __future__ import annotations

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
            }) + "\n")
    except Exception as e:
        log.warning(f"[supervisor] failed to log plan revision: {e}")


class SupervisorAgent:
    """
    Drives `plan_tasks()` -> sequential sub-task execution -> `revise_plan()`
    after each step, replacing the remaining plan or stopping early when the
    LLM supervisor says so.
    """

    def __init__(self, max_revisions: int = 3):
        self.max_revisions = max_revisions

    async def run(self, goal: str, persona: str = "jarvis", session_id: str = "__agents__") -> dict:
        plan = await plan_tasks(goal, persona)
        if not plan:
            return {
                "goal": goal, "reply": "Could not decompose task.",
                "plan": [], "results": [], "revisions": 0, "capped": False,
            }

        context: dict[str, Any] = {}
        completed_ids: set[str] = set()
        all_results: list[dict] = []
        remaining = list(plan)
        revisions = 0
        capped = False

        while remaining:
            ready = [t for t in remaining if set(t.get("depends_on", [])).issubset(completed_ids)]
            step = ready[0] if ready else remaining[0]

            result = await _execute_subtask(step, context, persona, session_id)
            all_results.append(result)
            completed_ids.add(result["id"])
            remaining.remove(step)

            if not remaining:
                break  # nothing left to revise

            if revisions >= self.max_revisions:
                capped = True
                continue  # cap reached: run out the rest of the (current) plan as-is

            try:
                import asyncio
                revised = await asyncio.wait_for(
                    revise_plan(goal, plan, all_results, result, persona, session_id),
                    timeout=REVISE_TIMEOUT_SEC,
                )
            except Exception as e:
                log.warning(f"[supervisor] revise_plan failed/timed out ({e}); continuing with current plan")
                revised = {"action": "continue", "new_plan": [], "reason": f"revise_plan error: {e}"}

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
                    _log_revision(session_id, goal, revisions, remaining, new_tail, revised.get("reason", ""))
                    plan = [r for r in plan if r.get("id") in completed_ids] + new_tail
                    remaining = new_tail
                # if new_plan was empty, treat like "continue" (nothing to replace with)
            # action == "continue": no change

        final_reply = await _synthesise(goal, all_results, persona, session_id)
        if capped:
            final_reply += "\n\n(Note: re-planning was capped after the maximum number of revisions; remaining steps ran as originally planned.)"

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
        }
