"""
Health & diagnostics endpoints.
GET  /health              -- overall system health
GET  /providers           -- per-provider / per-model status + full registry
GET  /models              -- full model catalog (all 104 models)
GET  /latency/stats       -- per-model P50/P95/mean latency (Phase 3)
GET  /models/stats        -- alias for /latency/stats
GET  /ab-test/results     -- A/B experiment results (Phase 3)
POST /ab-test/register    -- register a new A/B experiment at runtime (Phase 3)
"""
import json
import time
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.intelligence.router import get_router

router   = APIRouter(prefix="/api", tags=["health"])
settings = get_settings()

_START_TIME = time.time()


@router.get("/health")
async def health():
    uptime    = int(time.time() - _START_TIME)
    providers = settings.available_providers
    return {
        "status":               "online",
        "system":               "TRON-X",
        "version":              "1.0.0",
        "uptime_seconds":       uptime,
        "configured_providers": providers,
        "provider_count":       len(providers),
        "total_models":         104,
        "total_providers":      14,
    }


@router.get("/providers")
async def provider_status():
    return get_router().provider_status()


@router.get("/models")
async def list_models():
    catalog = json.loads(Path("config/models.json").read_text())
    all_models = []
    for provider, cfg in catalog["provider_configs"].items():
        for m in cfg.get("_models", []):
            all_models.append({
                "model":    m,
                "provider": provider,
                "context":  cfg.get("context_window", 32768),
                "rpm":      cfg.get("rpm_limit", 30),
                "active":   provider in settings.available_providers,
            })
    primary_map: dict[str, list[str]] = {}
    for cat, data in catalog["categories"].items():
        p = data["primary"]
        primary_map.setdefault(p, []).append(cat)
    for m in all_models:
        m["primary_for"] = primary_map.get(m["model"], [])
    return {
        "total":      len(all_models),
        "providers":  14,
        "categories": list(catalog["categories"].keys()),
        "models":     all_models,
    }


# Phase 3: Latency stats (two URL aliases)

async def _latency_stats_response():
    r = get_router()
    all_stats = r.latency_tracker.all_stats()
    if not all_stats:
        return {
            "message": "No latency data yet -- stats accumulate as requests are served.",
            "models": {},
            "total_tracked": 0,
            "best_p50_model": None,
            "best_p50_ms": None,
        }
    # Best model: lowest P50 with at least 3 samples
    candidates = [(m, s) for m, s in all_stats.items() if s["n"] >= 3 and s["p50"] is not None]
    if candidates:
        best_model, best_stats = min(candidates, key=lambda x: x[1]["p50"])
        best_p50_model = best_model
        best_p50_ms    = best_stats["p50"]
    else:
        best_p50_model = None
        best_p50_ms    = None
    return {
        "models":         all_stats,
        "total_tracked":  len(all_stats),
        "best_p50_model": best_p50_model,
        "best_p50_ms":    best_p50_ms,
    }


@router.get("/latency/stats")
async def latency_stats():
    """Per-model rolling latency stats (P50 / P95 / mean / sample count)."""
    return await _latency_stats_response()


@router.get("/models/stats")
async def models_stats():
    """Alias for /latency/stats — per-model performance metrics."""
    return await _latency_stats_response()


# Phase 3: A/B test endpoints

@router.get("/ab-test/results")
async def ab_test_results():
    """Current metrics for all registered A/B experiments."""
    r = get_router()
    results = r.ab_tests.results()
    if not results:
        return {"message": "No A/B experiments registered.", "experiments": {}}
    return {"experiments": results, "total": len(results)}


class ABVariant(BaseModel):
    model:  str   = Field(..., description="Full model ID e.g. 'groq/llama-3.3-70b-versatile'")
    weight: float = Field(..., gt=0, description="Relative traffic weight (will be normalised)")


class ABRegisterRequest(BaseModel):
    experiment_id: str             = Field(..., description="Unique experiment name")
    category:      str             = Field(..., description="Router category e.g. 'fast_chat'")
    variants:      List[ABVariant] = Field(..., min_length=2, max_length=8)
    traffic_pct:   float           = Field(1.0, ge=0.01, le=1.0,
                                          description="Fraction of requests in experiment (0.01-1.0)")


@router.post("/ab-test/register", status_code=201)
async def register_ab_test(req: ABRegisterRequest):
    """Register a new A/B experiment at runtime (no restart needed)."""
    r = get_router()
    catalog = r.catalog["categories"]
    if req.category not in catalog:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category '{req.category}'. Valid: {list(catalog.keys())}",
        )
    variants = [{"model": v.model, "weight": v.weight} for v in req.variants]
    r.ab_tests.register(
        experiment_id=req.experiment_id,
        variants=variants,
        category=req.category,
        traffic_pct=req.traffic_pct,
    )
    return {
        "registered":  req.experiment_id,
        "category":    req.category,
        "variants":    variants,
        "traffic_pct": req.traffic_pct,
    }
