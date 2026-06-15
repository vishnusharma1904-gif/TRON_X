"""
TRON-X Task Coordinator  (Phase 10)
-------------------------------------
Registry-based multi-agent dispatcher with parallel execution and SSE streaming.

Unlike the LLM-planned task_decomposer, TaskCoordinator lets the caller
explicitly select which registered agents to run — good for deterministic
pipelines and the HUD's agent palette UI.

Registry agents
  research_v2    ResearchAgentV2.run()
  research       ResearchAgent.run()
  python         executor.execute_python_safe()
  js             executor.execute_js()
  bash           executor.execute_bash()
  browser_scrape BrowserAgent scrape action
  screenshot     vision_screen.capture_screen()
  ocr_screen     vision_screen.ocr_screen()
  describe_screen vision_screen.describe_screen()
  system_info    control.get_system_info()
  processes      control.list_processes()
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncGenerator, Callable, Awaitable

from src.core.logger import log


# ---------------------------------------------------------------------------
# Agent registry
# Each entry: {"fn": async callable(payload: dict) -> dict, "description": str}
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, dict] = {}


def register_agent(name: str, description: str):
    """Decorator to register an async fn as a named agent."""
    def decorator(fn: Callable[[dict], Awaitable[dict]]):
        _REGISTRY[name] = {"fn": fn, "description": description}
        return fn
    return decorator


def list_agents() -> list[dict]:
    return [{"name": k, "description": v["description"]} for k, v in _REGISTRY.items()]


# ---------------------------------------------------------------------------
# Built-in registrations (lazy imports to avoid circular deps at module load)
# ---------------------------------------------------------------------------

@register_agent("research_v2", "Web research with provider cascade + optional 2nd hop")
async def _agent_research_v2(payload: dict) -> dict:
    from src.agents.research_agent import ResearchAgentV2
    return await ResearchAgentV2().run(
        payload.get("query", payload.get("input", "")),
        max_hops=payload.get("max_hops", 1),
    )


@register_agent("research", "Web research via ResearchAgent (legacy)")
async def _agent_research(payload: dict) -> dict:
    from src.agents.research_agent import ResearchAgent
    result = await ResearchAgent().run(
        payload.get("query", payload.get("input", "")),
        persona=payload.get("persona", "jarvis"),
    )
    return result if isinstance(result, dict) else {"result": result}


@register_agent("python", "Execute Python code in sandboxed AST-checked environment")
async def _agent_python(payload: dict) -> dict:
    from src.system.executor import execute_python_safe
    return await execute_python_safe(
        payload.get("code", payload.get("input", "")),
        timeout=payload.get("timeout", 15),
        auto_install=payload.get("auto_install", True),
    )


@register_agent("js", "Execute JavaScript via Node.js")
async def _agent_js(payload: dict) -> dict:
    from src.system.executor import execute_js
    return await execute_js(
        payload.get("code", payload.get("input", "")),
        timeout=payload.get("timeout", 15),
    )


@register_agent("bash", "Execute whitelisted bash commands")
async def _agent_bash(payload: dict) -> dict:
    from src.system.executor import execute_bash
    return await execute_bash(
        payload.get("code", payload.get("input", "")),
        timeout=payload.get("timeout", 15),
    )


@register_agent("browser_scrape", "Scrape a URL with the persistent Playwright browser")
async def _agent_browser_scrape(payload: dict) -> dict:
    from src.agents.browser_agent import BrowserAgent
    agent = await BrowserAgent.get()
    return await agent.scrape(payload.get("url", payload.get("input", "")))


@register_agent("screenshot", "Capture the primary monitor screenshot")
async def _agent_screenshot(payload: dict) -> dict:
    from src.vision import screen as vs
    return await vs.capture_screen(
        save_path=payload.get("save_path"),
        region=payload.get("region"),
        monitor=payload.get("monitor", 1),
        return_base64=payload.get("return_base64", False),
    )


@register_agent("ocr_screen", "Run OCR on the current screen")
async def _agent_ocr_screen(payload: dict) -> dict:
    from src.vision import screen as vs
    return await vs.ocr_screen(
        region=payload.get("region"),
        engine=payload.get("engine", "auto"),
    )


@register_agent("describe_screen", "Describe current screen contents via vision LLM")
async def _agent_describe_screen(payload: dict) -> dict:
    from src.vision import screen as vs
    return await vs.describe_screen(
        region=payload.get("region"),
        prompt=payload.get("prompt", "Describe what you see on this screen in detail."),
        return_base64=payload.get("return_base64", False),
    )


@register_agent("system_info", "Get OS, CPU, memory, and disk info")
async def _agent_system_info(payload: dict) -> dict:
    from src.system.control import get_system_info
    return await get_system_info()


@register_agent("processes", "List running processes sorted by CPU or memory")
async def _agent_processes(payload: dict) -> dict:
    from src.system.control import list_processes
    return await list_processes(sort_by=payload.get("sort_by", "cpu"))


@register_agent("security_scan",
                "Authorized recon & vulnerability scanning (scope-gated pentest)")
async def _agent_security(payload: dict) -> dict:
    from src.agents.security_agent import SecurityAgent
    return await SecurityAgent().run(
        payload.get("query", payload.get("input", payload.get("request", ""))),
        engagement_id=payload.get("engagement_id"),
    )


# ---------------------------------------------------------------------------
# TaskCoordinator
# ---------------------------------------------------------------------------

class TaskCoordinator:
    """
    Runs named agents in parallel (or sequentially) and collects results.
    Each agent receives a payload dict that can contain any inputs it needs.
    """

    @staticmethod
    def registry() -> list[dict]:
        return list_agents()

    @staticmethod
    async def run_one(agent_name: str, payload: dict) -> dict:
        """Run a single registered agent and return its result."""
        entry = _REGISTRY.get(agent_name)
        if not entry:
            return {"agent": agent_name, "success": False, "error": f"Unknown agent: {agent_name}"}
        t0 = time.monotonic()
        try:
            result = await entry["fn"](payload)
            elapsed = round((time.monotonic() - t0) * 1000)
            return {"agent": agent_name, "success": True, "result": result, "elapsed_ms": elapsed}
        except Exception as e:
            elapsed = round((time.monotonic() - t0) * 1000)
            log.error(f"[coordinator] Agent '{agent_name}' failed: {e}")
            return {"agent": agent_name, "success": False, "error": str(e), "elapsed_ms": elapsed}

    @staticmethod
    async def run_parallel(tasks: list[dict]) -> list[dict]:
        """
        Run multiple agent tasks concurrently.

        tasks: list of {"agent": str, "payload": dict, "id": str (optional)}
        Returns list of results in the same order.
        """
        async def _run(task: dict) -> dict:
            agent_name = task.get("agent", "")
            payload    = task.get("payload", {})
            task_id    = task.get("id", agent_name)
            result     = await TaskCoordinator.run_one(agent_name, payload)
            return {**result, "task_id": task_id}

        return await asyncio.gather(*[_run(t) for t in tasks])

    @staticmethod
    async def run_sequential(tasks: list[dict], share_context: bool = False) -> list[dict]:
        """
        Run tasks one at a time. If share_context=True, each task's result
        is injected into the next task's payload as "previous_result".
        """
        results: list[dict] = []
        prev_result: Any = None
        for task in tasks:
            payload = dict(task.get("payload", {}))
            if share_context and prev_result is not None:
                payload["previous_result"] = prev_result
            result = await TaskCoordinator.run_one(task.get("agent", ""), payload)
            result["task_id"] = task.get("id", task.get("agent", ""))
            results.append(result)
            prev_result = result.get("result")
        return results

    @staticmethod
    async def stream_parallel(
        tasks: list[dict],
    ) -> AsyncGenerator[str, None]:
        """
        Run tasks in parallel and yield SSE events as each completes.
        Events: agent_start | agent_result | agent_error | done
        """
        def _evt(payload: dict) -> str:
            return f"data: {json.dumps(payload)}\n\n"

        # Fire all tasks and emit start events
        total = len(tasks)
        pending: dict[str, asyncio.Task] = {}

        for task in tasks:
            agent_name = task.get("agent", "")
            task_id    = task.get("id", agent_name)
            payload    = task.get("payload", {})
            pending[task_id] = asyncio.create_task(
                TaskCoordinator.run_one(agent_name, payload)
            )
            yield _evt({"type": "agent_start", "task_id": task_id, "agent": agent_name})

        # Collect completions as they arrive
        completed = 0
        futures = {asyncio.ensure_future(coro): tid for tid, coro in pending.items()}
        remaining = dict(futures)

        while remaining:
            done, _ = await asyncio.wait(
                list(remaining.keys()), return_when=asyncio.FIRST_COMPLETED
            )
            for fut in done:
                task_id = remaining.pop(fut)
                result = fut.result()
                completed += 1
                if result.get("success"):
                    yield _evt({
                        "type": "agent_result",
                        "task_id": task_id,
                        "agent": result.get("agent"),
                        "result": result.get("result"),
                        "elapsed_ms": result.get("elapsed_ms"),
                        "completed": completed,
                        "total": total,
                    })
                else:
                    yield _evt({
                        "type": "agent_error",
                        "task_id": task_id,
                        "agent": result.get("agent"),
                        "error": result.get("error"),
                        "elapsed_ms": result.get("elapsed_ms"),
                        "completed": completed,
                        "total": total,
                    })

        yield _evt({"type": "done", "total": total, "completed": completed})
