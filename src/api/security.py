"""
TRON-X Security API  (Phase: SecurityAgent)
-------------------------------------------
Scope-gated recon & vulnerability-scan endpoints.

    GET  /api/security/engagements      list authorized engagements
    POST /api/security/scope/check      dry-run authorization check
    POST /api/security/recon            Tier 0 passive recon
    POST /api/security/scan             Tier 1 active scan (scope-gated)
    GET  /api/security/audit            recent audit-log events

All routes inherit the app-wide auth middleware + rate limiter.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.security.scope import load_scope
from src.security.audit import read_audit
from src.agents.security_agent import SecurityAgent

router = APIRouter(prefix="/api/security", tags=["security"])


class ScopeCheckIn(BaseModel):
    target: str
    tier: int = Field(default=0, ge=0, le=2)


class ReconIn(BaseModel):
    target: str


class ScanIn(BaseModel):
    target: str
    engagement_id: Optional[str] = None
    request: Optional[str] = Field(
        default=None,
        description="Optional NL command, e.g. 'nmap ports 1-1024'. "
                    "If omitted, runs a full Tier-1 sweep.",
    )


@router.get("/engagements")
async def engagements():
    scope = load_scope()
    return {
        "engagements": [
            {
                "id": e.id, "owner": e.owner, "authorized_by": e.authorized_by,
                "expires": e.expires.isoformat() if e.expires else None,
                "active": e.is_active(), "max_tier": e.max_tier,
                "targets": e.targets,
            }
            for e in scope.engagements
        ]
    }


@router.post("/scope/check")
async def scope_check(body: ScopeCheckIn):
    decision = load_scope().check(body.target, body.tier)
    return {
        "target": body.target, "tier": body.tier,
        "allowed": decision.allowed, "reason": decision.reason,
        "engagement_id": decision.engagement_id,
    }


@router.post("/recon")
async def recon(body: ReconIn):
    result = await SecurityAgent().run(f"recon {body.target}")
    if not result.get("ok") and result.get("denied"):
        raise HTTPException(status_code=403, detail=result["reply"])
    return result


@router.post("/scan")
async def scan(body: ScanIn):
    cmd = body.request or f"scan {body.target}"
    if body.target not in cmd:
        cmd = f"{cmd} {body.target}"
    result = await SecurityAgent().run(cmd, engagement_id=body.engagement_id)
    if not result.get("ok") and result.get("denied"):
        raise HTTPException(status_code=403, detail=result["reply"])
    return result


@router.get("/audit")
async def audit_log(limit: int = 200):
    return {"events": read_audit(limit=limit)}
