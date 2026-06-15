"""
TRON-X Agents API
Endpoints for multi-agent pipeline, research, code execution,
CAD generation, and scheduled task management.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional

from src.core.logger import log

router = APIRouter(prefix="/api/agents", tags=["agents"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class AgentTaskReq(BaseModel):
    goal:         str
    persona:      str = "jarvis"
    session_id:   str = "__agents__"
    supervised:   bool = False
    mode:         str = Field(default="sequential", description="sequential | parallel")
    max_parallel: int = Field(default=4, ge=1, le=8)
    attachments:  Optional[list[dict]] = None

class ReasonReq(BaseModel):
    """[Phase 38] Deliberative reasoning request."""
    question:    str
    samples:     int   = Field(default=3, ge=1, le=7)
    reflect:     bool  = True
    verify:      bool  = True
    temperature: float = Field(default=0.7, ge=0.0, le=1.5)
    persona:     str   = "jarvis"
    session_id:  str   = "__reasoner__"
    pro:         bool  = False
    attachments: Optional[list[dict]] = None

class ResearchReq(BaseModel):
    query:   str
    persona: str = "jarvis"

class CodeReq(BaseModel):
    task:    str
    persona: str = "jarvis"

class CADReq(BaseModel):
    description: str
    persona:     str = "jarvis"
    output_dir:  str = "memory/cache/cad"

class ScheduleAddReq(BaseModel):
    job_id:      str
    task_prompt: str
    schedule:    str = Field(..., description="Natural language or cron expression")
    persona:     str = "jarvis"

class VisionReq(BaseModel):
    prompt:     str
    image_path: Optional[str] = None
    image_b64:  Optional[str] = None
    image_mime: str = "image/png"
    persona:    str = "jarvis"

class ScheduleRemoveReq(BaseModel):
    job_id: str


# ---------------------------------------------------------------------------
# Analytics helper (fire-and-forget)
# ---------------------------------------------------------------------------

def _record_agent(agent: str, result: dict) -> None:
    """Queue an agent analytics event without blocking the response."""
    try:
        from src.analytics.collector import get_collector
        asyncio.create_task(
            get_collector().record_agent(
                agent_name=agent,
                task=result.get("task", agent),
                success=bool(result.get("success", True)),
                latency_ms=float(result.get("elapsed_ms", result.get("latency_ms", 0))),
                model=result.get("model", "unknown"),
            )
        )
    except Exception:
        pass   # analytics must never break agent calls


# ---------------------------------------------------------------------------
# Multi-agent pipeline
# ---------------------------------------------------------------------------

@router.post("/run")
async def run_agent_pipeline(req: AgentTaskReq):
    """
    Decompose a complex goal into sub-tasks and execute them
    using specialised agents (research, code, iot, memory, etc.).
    """
    if req.supervised and req.mode == "parallel":
        from src.agents.parallel_supervisor import ParallelSupervisorAgent
        result = await ParallelSupervisorAgent(max_parallel=req.max_parallel).run(
            goal=req.goal,
            persona=req.persona,
            session_id=req.session_id,
        )
    elif req.supervised:
        from src.agents.supervisor import SupervisorAgent
        result = await SupervisorAgent().run(
            goal=req.goal,
            persona=req.persona,
            session_id=req.session_id,
        )
    else:
        from src.agents.task_decomposer import run_agent_pipeline
        result = await run_agent_pipeline(
            goal=req.goal,
            persona=req.persona,
            session_id=req.session_id,
        )
    return result


@router.post("/run/stream")
async def stream_agent_pipeline_api(req: AgentTaskReq):
    """
    Stream the multi-agent task decomposer (SSE).
    """
    goal = req.goal
    if req.attachments:
        from src.ingestion.attachments import Attachment, merge_for_prompt
        atts = [Attachment(**a) for a in req.attachments]
        text_block, _ = merge_for_prompt(atts)
        if text_block:
            goal = f"{goal}\n\n{text_block}"
            
    from src.agents.task_decomposer import stream_agent_pipeline
    return StreamingResponse(
        stream_agent_pipeline(
            goal=goal,
            persona=req.persona,
            session_id=req.session_id,
            max_parallel=req.max_parallel,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
# ---------------------------------------------------------------------------
# Deliberative reasoning (Phase 38)
# ---------------------------------------------------------------------------

@router.post("/reason")
async def reason(req: ReasonReq):
    """
    Deliberative reasoning: sample N independent reasoning paths
    (self-consistency), majority-vote/aggregate, then reflect and verify.
    Returns the answer with a confidence score and a stage trace.
    """
    question = req.question
    if req.attachments:
        from src.ingestion.attachments import Attachment, merge_for_prompt
        atts = [Attachment(**a) for a in req.attachments]
        text_block, _ = merge_for_prompt(atts)
        if text_block:
            question = f"{question}\n\n{text_block}"
            
    from src.intelligence.reasoning import DeliberativeReasoner
    reasoner = DeliberativeReasoner(
        samples=req.samples,
        reflect=req.reflect,
        verify=req.verify,
        temperature=req.temperature,
    )
    return await reasoner.reason(
        question, persona=req.persona, session_id=req.session_id,
        intent="reasoning_pro" if req.pro else "reasoning"
    )


# ---------------------------------------------------------------------------
# Research agent
# ---------------------------------------------------------------------------

@router.post("/research")
async def research(req: ResearchReq):
    from src.agents.research_agent import ResearchAgent
    result = await ResearchAgent().run(req.query, persona=req.persona)
    return {"query": req.query, "result": result}


# ---------------------------------------------------------------------------
# Code agent
# ---------------------------------------------------------------------------

@router.post("/code")
async def code_agent(req: CodeReq):
    from src.agents.code_agent import CodeAgent
    result = await CodeAgent().run(req.task, persona=req.persona)
    return {"task": req.task, "result": result}


# ---------------------------------------------------------------------------
# CAD agent
# ---------------------------------------------------------------------------

@router.post("/cad")
async def cad_agent(req: CADReq):
    from src.agents.cad_agent import CADAgent
    result = await CADAgent().run(
        req.description, output_dir=req.output_dir, persona=req.persona
    )
    return {"description": req.description, "result": result}


# ---------------------------------------------------------------------------
# Vision agent
# ---------------------------------------------------------------------------

@router.post("/vision")
async def vision_agent(req: VisionReq):
    """Analyse an image with a text prompt."""
    from src.agents.vision_agent import VisionAgent
    result = await VisionAgent().run(
        prompt=req.prompt,
        image_path=req.image_path,
        image_b64=req.image_b64,
        image_mime=req.image_mime,
        persona=req.persona,
    )
    return {"prompt": req.prompt, "result": result}


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

@router.get("/schedule")
async def list_scheduled_jobs():
    from src.agents.scheduler_agent import get_scheduler
    return {"jobs": get_scheduler().list_jobs()}


@router.post("/schedule")
async def add_scheduled_task(req: ScheduleAddReq):
    from src.agents.scheduler_agent import get_scheduler
    from src.intelligence.orchestrator import get_orchestrator

    sched = get_scheduler()
    parsed = await sched.parse_nl_schedule(req.schedule)
    t = parsed.get("type", "unknown")
    orch = get_orchestrator()

    async def _job_fn():
        result = await orch.chat(
            req.task_prompt, f"__sched_{req.job_id}__",
            "chat", req.persona, max_tokens=600,
        )
        log.info("[sched] %s: %s", req.job_id, result.get("reply", "")[:80])

    def _sync_wrapper():
        asyncio.create_task(_job_fn())

    if t == "cron":
        result = sched.add_cron_job(
            req.job_id, _sync_wrapper, parsed["expr"],
            description=req.task_prompt[:80],
        )
    elif t == "interval":
        result = sched.add_interval_job(
            req.job_id, _sync_wrapper,
            seconds=parsed.get("seconds", 3600),
            description=req.task_prompt[:80],
        )
    elif t == "oneshot":
        result = sched.add_oneshot_job(
            req.job_id, _sync_wrapper,
            delay_seconds=parsed.get("delay_seconds", 60),
            description=req.task_prompt[:80],
        )
    else:
        raise HTTPException(400, f"Could not parse schedule: {req.schedule}")

    return {**result, "parsed_schedule": parsed}


@router.delete("/schedule/{job_id}")
async def remove_scheduled_task(job_id: str):
    from src.agents.scheduler_agent import get_scheduler
    return get_scheduler().remove_job(job_id)


@router.post("/schedule/briefing")
async def register_daily_briefing(hour: int = 8, persona: str = "jarvis"):
    from src.agents.scheduler_agent import get_scheduler
    return get_scheduler().register_daily_briefing(hour=hour, persona=persona)


# ---------------------------------------------------------------------------
# Phase 9 -- ResearchAgentV2 with provider cascade + SSE streaming
# ---------------------------------------------------------------------------

from src.agents.research_agent import ResearchAgentV2

class ResearchV2Req(BaseModel):
    query:    str
    max_hops: int = Field(default=1, ge=1, le=2)


@router.post("/research/v2")
async def research_v2(req: ResearchV2Req):
    agent = ResearchAgentV2()
    return await agent.run(req.query, req.max_hops)


@router.post("/research/stream")
async def research_stream(req: ResearchV2Req):
    agent = ResearchAgentV2()
    return StreamingResponse(
        agent.stream(req.query, req.max_hops),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Phase 10 -- TaskCoordinator: registry-based parallel agent dispatch + SSE
# ---------------------------------------------------------------------------

from src.agents.coordinator import TaskCoordinator

class CoordinateTask(BaseModel):
    agent:   str
    payload: dict = Field(default_factory=dict)
    id:      Optional[str] = None

class CoordinateReq(BaseModel):
    tasks:         list[CoordinateTask]
    mode:          str  = Field(default="parallel", description="parallel | sequential")
    share_context: bool = False

class CoordinateSingleReq(BaseModel):
    agent:   str
    payload: dict = Field(default_factory=dict)


@router.get("/coordinate/registry")
async def coordinate_registry():
    """List all registered agents available for coordination."""
    return {"agents": TaskCoordinator.registry()}


@router.post("/coordinate/single")
async def coordinate_single(req: CoordinateSingleReq):
    """Run a single named agent, recording analytics on completion."""
    result = await TaskCoordinator.run_one(req.agent, req.payload)
    _record_agent(req.agent, result)
    return result


@router.post("/coordinate")
async def coordinate(req: CoordinateReq):
    """
    Run multiple agents in parallel or sequentially.
    mode=parallel: asyncio.gather, results tracked by arrival order
    mode=sequential: one at a time; set share_context=True to pipe results forward
    """
    tasks = [t.model_dump() for t in req.tasks]
    if req.mode == "sequential":
        results = await TaskCoordinator.run_sequential(tasks, share_context=req.share_context)
    else:
        results = await TaskCoordinator.run_parallel(tasks)
    # fire-and-forget analytics for each completed task
    for t_def, r in zip(req.tasks, results):
        _record_agent(t_def.agent, r)
    return {
        "mode":      req.mode,
        "total":     len(results),
        "completed": sum(1 for r in results if r.get("success")),
        "failed":    sum(1 for r in results if not r.get("success")),
        "results":   results,
    }


@router.post("/coordinate/stream")
async def coordinate_stream(req: CoordinateReq):
    """
    SSE stream -- runs all tasks in parallel and emits per-agent events
    as each one completes: agent_start | agent_result | agent_error | done
    """
    tasks = [t.model_dump() for t in req.tasks]
    return StreamingResponse(
        TaskCoordinator.stream_parallel(tasks),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
