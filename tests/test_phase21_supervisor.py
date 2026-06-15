"""
Phase 21 — Stateful Supervisor & Dynamic Plan Revision
Standalone test (see TRONX_PHASE21-36_PROGRESS_HANDOFF.md §3 for conventions).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

if "chromadb" not in sys.modules:
    sys.modules["chromadb"] = MagicMock()
    sys.modules["chromadb.config"] = MagicMock()

from src.agents import task_decomposer as td  # noqa: E402
from src.agents import supervisor as sup  # noqa: E402

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


def step(id_: str, deps: list[str] | None = None) -> dict:
    return {"id": id_, "type": "chat", "description": f"do {id_}",
            "depends_on": deps or [], "parallel": False}


# =============================================================================
print("\n=== _parse_revision: JSON parsing robustness ===")
# =============================================================================

r = td._parse_revision('{"action": "continue", "new_plan": [], "reason": "ok"}')
check("plain valid JSON -> continue", r["action"] == "continue", r)

r = td._parse_revision('```json\n{"action":"done","new_plan":[],"reason":"x"}\n```')
check("code-fenced JSON -> done", r["action"] == "done", r)

r = td._parse_revision(
    '{"action":"replace_remaining","new_plan":[{"id":"x","type":"chat",'
    '"description":"d","depends_on":[],"parallel":false},],"reason":"y",}'
)
check("trailing commas handled -> replace_remaining", r["action"] == "replace_remaining", r)
check("trailing commas: new_plan parsed", len(r.get("new_plan", [])) == 1, r)

r = td._parse_revision(
    'Sure, here is the revision:\n'
    '{"action":"continue","new_plan":[],"reason":"fine"}\n'
    'Let me know if anything else is needed.'
)
check("JSON object embedded in prose -> continue", r["action"] == "continue", r)

r = td._parse_revision("I think we should keep going, no changes needed.")
check("unparseable plain text -> defaults to continue", r["action"] == "continue", r)
check("unparseable plain text -> empty new_plan", r["new_plan"] == [], r)

r = td._parse_revision('{"action":"foo","new_plan":[]}')
check("invalid action value -> defaults to continue", r["action"] == "continue", r)

r = td._parse_revision("")
check("empty string -> defaults to continue", r["action"] == "continue", r)


# =============================================================================
print("\n=== revise_plan(): LLM call + error handling ===")
# =============================================================================


async def _revise_plan_test():
    fake_orch_mod = MagicMock()
    fake_orch = MagicMock()
    fake_orch.chat = AsyncMock(return_value={
        "reply": '{"action":"continue","new_plan":[],"reason":"on track"}'
    })
    fake_orch_mod.get_orchestrator.return_value = fake_orch

    with patch.dict(sys.modules, {"src.intelligence.orchestrator": fake_orch_mod}):
        result = await td.revise_plan("goal", [step("a")], [], {"id": "a", "success": True})
        check("revise_plan returns parsed continue", result["action"] == "continue", result)

        # LLM call raises -> degrade to continue, never raises
        fake_orch.chat = AsyncMock(side_effect=RuntimeError("llm down"))
        result = await td.revise_plan("goal", [step("a")], [], {"id": "a", "success": True})
        check("revise_plan survives LLM error", result["action"] == "continue", result)
        check("revise_plan error reason mentions error", "revise_plan error" in result["reason"], result)


asyncio.run(_revise_plan_test())


# =============================================================================
print("\n=== SupervisorAgent.run(): single-step plan (no revision needed) ===")
# =============================================================================


async def _single_step_test():
    plan = [step("only")]

    with patch.object(sup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(sup, "_execute_subtask", new=AsyncMock(
             return_value={"id": "only", "type": "chat", "success": True, "result": "ok"})), \
         patch.object(sup, "_synthesise", new=AsyncMock(return_value="final answer")), \
         patch.object(sup, "revise_plan", new=AsyncMock(side_effect=AssertionError("should not be called"))):

        result = await sup.SupervisorAgent().run("goal")
        check("single-step: no revisions", result["revisions"] == 0, result)
        check("single-step: not capped", result["capped"] is False, result)
        check("single-step: reply is synthesis", result["reply"] == "final answer", result)
        check("single-step: 1 result", len(result["results"]) == 1, result)


asyncio.run(_single_step_test())


# =============================================================================
print("\n=== SupervisorAgent.run(): step failure -> replace_remaining ===")
# =============================================================================


async def _replace_remaining_test():
    plan = [step("a"), step("b"), step("c")]
    revisions_jsonl = "memory/cache/plan_revisions.jsonl"
    os.makedirs(os.path.dirname(revisions_jsonl), exist_ok=True)
    open(revisions_jsonl, "w").close()  # truncate (os.remove fails on this FUSE mount)

    async def fake_execute(task, context, persona, session_id):
        if task["id"] == "b":
            return {"id": "b", "type": "chat", "success": False, "error": "boom"}
        return {"id": task["id"], "type": "chat", "success": True, "result": f"{task['id']}-ok"}

    revise_calls = []

    async def fake_revise(goal, plan_, completed, last_result, persona="jarvis", session_id="__planner__"):
        revise_calls.append(last_result)
        if last_result["id"] == "b" and not last_result.get("success", True):
            return {"action": "replace_remaining", "new_plan": [step("d")], "reason": "b failed, retry differently"}
        return {"action": "continue", "new_plan": [], "reason": "fine"}

    with patch.object(sup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(sup, "_execute_subtask", new=fake_execute), \
         patch.object(sup, "_synthesise", new=AsyncMock(return_value="final answer")), \
         patch.object(sup, "revise_plan", new=fake_revise):

        result = await sup.SupervisorAgent().run("goal", session_id="__test21__")

        check("fallthrough: tried original step b first",
              any(r["id"] == "b" for r in result["results"]), result["results"])
        check("fallthrough: executed replacement step d",
              any(r["id"] == "d" for r in result["results"]), result["results"])
        check("fallthrough: original step c dropped",
              not any(r["id"] == "c" for r in result["results"]), result["results"])
        check("fallthrough: 1 revision recorded", result["revisions"] == 1, result)
        check("fallthrough: not capped", result["capped"] is False, result)
        check("fallthrough: revise_plan called twice (after a, after b)", len(revise_calls) == 2, revise_calls)

    check("plan_revisions.jsonl written", os.path.exists(revisions_jsonl))
    if os.path.exists(revisions_jsonl):
        with open(revisions_jsonl) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        check("plan_revisions.jsonl: exactly 1 line (only real revisions logged)", len(lines) == 1, lines)
        if lines:
            entry = lines[0]
            check("plan_revisions.jsonl: has goal/session_id/revision_n",
                  entry.get("goal") == "goal" and entry.get("session_id") == "__test21__"
                  and entry.get("revision_n") == 1, entry)
            check("plan_revisions.jsonl: new_plan is the replacement step",
                  entry.get("new_plan") and entry["new_plan"][0]["id"] == "d", entry)


asyncio.run(_replace_remaining_test())


# =============================================================================
print("\n=== SupervisorAgent.run(): early 'done' stops remaining steps ===")
# =============================================================================


async def _done_test():
    plan = [step("a"), step("b"), step("c")]
    revisions_jsonl = "memory/cache/plan_revisions.jsonl"
    os.makedirs(os.path.dirname(revisions_jsonl), exist_ok=True)
    open(revisions_jsonl, "w").close()  # truncate (os.remove fails on this FUSE mount)

    async def fake_execute(task, context, persona, session_id):
        return {"id": task["id"], "type": "chat", "success": True, "result": f"{task['id']}-ok"}

    async def fake_revise(goal, plan_, completed, last_result, persona="jarvis", session_id="__planner__"):
        return {"action": "done", "new_plan": [], "reason": "goal already achieved"}

    with patch.object(sup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(sup, "_execute_subtask", new=fake_execute), \
         patch.object(sup, "_synthesise", new=AsyncMock(return_value="final answer")), \
         patch.object(sup, "revise_plan", new=fake_revise):

        result = await sup.SupervisorAgent().run("goal", session_id="__test21b__")

        check("done: only step a executed", [r["id"] for r in result["results"]] == ["a"], result["results"])
        check("done: 1 revision recorded", result["revisions"] == 1, result)

    if os.path.exists(revisions_jsonl):
        with open(revisions_jsonl) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        check("done: plan_revisions.jsonl logs the early stop",
              len(lines) == 1 and lines[0]["new_plan"] == [] and lines[0]["old_plan"] == [step("b"), step("c")],
              lines)


asyncio.run(_done_test())


# =============================================================================
print("\n=== SupervisorAgent.run(): max_revisions cap ===")
# =============================================================================


async def _cap_test():
    plan = [step("s0"), step("s_orig2")]

    async def fake_execute(task, context, persona, session_id):
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    revise_calls = {"n": 0}

    async def fake_revise(goal, plan_, completed, last_result, persona="jarvis", session_id="__planner__"):
        n = revise_calls["n"]
        revise_calls["n"] += 1
        new_plan = [step(f"s{n+1}"), step(f"s{n+1}b")]
        return {"action": "replace_remaining", "new_plan": new_plan, "reason": f"rev{n}"}

    with patch.object(sup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(sup, "_execute_subtask", new=fake_execute), \
         patch.object(sup, "_synthesise", new=AsyncMock(return_value="final answer")), \
         patch.object(sup, "revise_plan", new=fake_revise):

        result = await sup.SupervisorAgent(max_revisions=3).run("goal", session_id="__test21c__")

        check("cap: revise_plan called exactly max_revisions times", revise_calls["n"] == 3, revise_calls)
        check("cap: revisions == 3", result["revisions"] == 3, result)
        check("cap: capped flag set", result["capped"] is True, result)
        check("cap: 5 steps executed total", len(result["results"]) == 5,
              [r["id"] for r in result["results"]])
        check("cap: reply notes re-planning was capped", "capped" in result["reply"].lower(), result["reply"])


asyncio.run(_cap_test())


# =============================================================================
print("\n=== SupervisorAgent.run(): revise_plan exception doesn't crash the loop ===")
# =============================================================================


async def _revise_exception_test():
    plan = [step("a"), step("b")]

    async def fake_execute(task, context, persona, session_id):
        return {"id": task["id"], "type": "chat", "success": True, "result": "ok"}

    async def fake_revise(*args, **kwargs):
        raise RuntimeError("revise boom")

    with patch.object(sup, "plan_tasks", new=AsyncMock(return_value=plan)), \
         patch.object(sup, "_execute_subtask", new=fake_execute), \
         patch.object(sup, "_synthesise", new=AsyncMock(return_value="final answer")), \
         patch.object(sup, "revise_plan", new=fake_revise):

        result = await sup.SupervisorAgent().run("goal", session_id="__test21d__")

        check("revise exception: both steps still executed", len(result["results"]) == 2, result["results"])
        check("revise exception: no revisions recorded", result["revisions"] == 0, result)
        check("revise exception: not capped", result["capped"] is False, result)


asyncio.run(_revise_exception_test())


# =============================================================================
print("\n=== API wiring: supervised flag + SupervisorAgent routing ===")
# =============================================================================

agents_src = open("src/api/agents.py").read()
check("AgentTaskReq has 'supervised' field", "supervised: bool = False" in agents_src)
check("/run endpoint imports SupervisorAgent", "from src.agents.supervisor import SupervisorAgent" in agents_src)
check("/run endpoint branches on req.supervised", "if req.supervised:" in agents_src)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
