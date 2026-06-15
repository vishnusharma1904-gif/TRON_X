"""
Phase 28 verification: Proactive Cron Analytics & Diagnostic Self-Healing.

Standalone script (no pytest dependency assumed) -- run from the repo root:
    python3 tests/test_phase28_self_healing.py

Exercises:
  - HealthTracker.get_status_summary()                       (router.py)
  - SmartRouter.bias_fallback_chain / _check_bias_revert /
    bias_status, and their effect on _get_chain() ordering
    (Step 1.5)                                                (router.py)
  - self_healing._pick_healthy_model                          (self_healing.py)
  - self_healing.run_health_check() -- RAM / disk / circuit-
    breaker remediation, anti-thrashing, diagnostics logging  (self_healing.py)
  - self_healing._log_diagnostic ring-buffer trimming +
    get_diagnostics_log                                       (self_healing.py)
  - ChromaManager.delete_old_conversations -- only touches
    the conversations collection                              (chroma_db.py)
  - config.py Phase 28 settings + system_health.py route
    registration
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)  # so relative paths (config/models.json, memory/cache/...) resolve

# chromadb is a heavy optional dependency (not installed in this sandbox) that
# chroma_db.py imports at module level. Its real behavior is irrelevant to the
# Phase 28 logic under test (we drive ChromaManager methods against a fake
# collection), so stub it out before importing chroma_db. See test_phase23
# for precedent.
if "chromadb" not in sys.modules:
    chromadb_mock = MagicMock()
    chromadb_config_mock = MagicMock()
    chromadb_mock.config = chromadb_config_mock
    sys.modules["chromadb"] = chromadb_mock
    sys.modules["chromadb.config"] = chromadb_config_mock

from src.core.config import get_settings  # noqa: E402
from src.intelligence import router as router_mod  # noqa: E402
from src.intelligence.router import HealthTracker, SmartRouter  # noqa: E402
from src.memory import chroma_db as chroma_mod  # noqa: E402
from src.system import self_healing  # noqa: E402

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


# =============================================================================
# 1. HealthTracker.get_status_summary()
# =============================================================================
print("== HealthTracker.get_status_summary ==")

ht = HealthTracker(failure_threshold=3, cooldown_seconds=120)
summary = ht.get_status_summary()
check("fresh tracker: tripped_models == []", summary["tripped_models"] == [], detail=str(summary))
check("fresh tracker: trip_counts == {}", summary["trip_counts"] == {}, detail=str(summary))
check("threshold reflects constructor", summary["threshold"] == 3)
check("cooldown_seconds reflects constructor", summary["cooldown_seconds"] == 120)

# Below threshold: shows up in trip_counts but not tripped yet
ht.mark_failure("modelA")
ht.mark_failure("modelA")
summary = ht.get_status_summary()
check("flaky model (2/3 failures) -> trip_counts", summary["trip_counts"].get("modelA") == 2, detail=str(summary))
check("flaky model (2/3 failures) -> not yet tripped", "modelA" not in summary["tripped_models"], detail=str(summary))

# 3rd failure crosses the threshold
ht.mark_failure("modelA")
summary = ht.get_status_summary()
check("3rd failure trips circuit", "modelA" in summary["tripped_models"], detail=str(summary))
detail_a = next((d for d in summary["tripped_detail"] if d["model"] == "modelA"), None)
check(
    "tripped_detail has 0 <= cooldown_remaining_s <= 120",
    detail_a is not None and 0 <= detail_a["cooldown_remaining_s"] <= 120,
    detail=str(detail_a),
)

# mark_success clears both trip and trip_count
ht.mark_success("modelA")
summary = ht.get_status_summary()
check("mark_success clears tripped state", "modelA" not in summary["tripped_models"], detail=str(summary))
check("mark_success resets trip_counts", "modelA" not in summary["trip_counts"], detail=str(summary))


# =============================================================================
# 2. SmartRouter.bias_fallback_chain / _check_bias_revert / bias_status
#    and their effect on _get_chain() (Step 1.5)
#
# NOTE: "reasoning" is used as the test category because it has no A/B
# experiment registered (_seed_ab_experiments only wires up "fast_chat" and
# "coding") and is not in _LATENCY_SENSITIVE, so _get_chain("reasoning") is
# fully deterministic given a fixed health/bias state -- no random.random()
# in the mix.
# =============================================================================
print("\n== SmartRouter.bias_fallback_chain / _get_chain Step 1.5 ==")

router = SmartRouter()
baseline = router._get_chain("reasoning")
check("baseline 'reasoning' chain non-empty", len(baseline) > 0, detail=str(baseline))
check("no bias active initially", router.bias_status() == {"active": False}, detail=str(router.bias_status()))

target = baseline[-1] if len(baseline) > 1 else baseline[0]
result = router.bias_fallback_chain(target, ttl_seconds=1800)
check("bias_fallback_chain returns biased_model", result["biased_model"] == target, detail=str(result))
check("bias_fallback_chain returns ttl_seconds", result["ttl_seconds"] == 1800, detail=str(result))

status = router.bias_status()
check("bias_status active after bias_fallback_chain", status.get("active") is True, detail=str(status))
check("bias_status reports biased_model", status.get("biased_model") == target, detail=str(status))
check("bias_status remaining_s > 0", status.get("remaining_s", -1) > 0, detail=str(status))

biased_chain = router._get_chain("reasoning")
check("biased model now first in chain", biased_chain[0] == target, detail=str(biased_chain))
check("biased chain is a reordering of baseline (same models)", set(biased_chain) == set(baseline), detail=str(biased_chain))


# -- TTL-based auto-revert ----------------------------------------------------
print("\n== bias auto-revert: TTL expiry ==")

router2 = SmartRouter()
baseline2 = router2._get_chain("reasoning")
target2 = baseline2[-1] if len(baseline2) > 1 else baseline2[0]

router2.bias_fallback_chain(target2, ttl_seconds=0)
check("bias active immediately after bias_fallback_chain(ttl=0)", router2.bias_status().get("active") is True)

time.sleep(0.01)  # ensure monotonic() has advanced past _bias_until
chain_after = router2._get_chain("reasoning")  # _get_chain calls _check_bias_revert internally
check("TTL=0 reverts on next _get_chain call", router2.bias_status() == {"active": False}, detail=str(router2.bias_status()))
check("chain returns to baseline after TTL revert", chain_after == baseline2, detail=str(chain_after))


# -- Recovery-based auto-revert -----------------------------------------------
print("\n== bias auto-revert: tripped-model recovery ==")

router3 = SmartRouter()
baseline3 = router3._get_chain("reasoning")
target3 = baseline3[-1] if len(baseline3) > 1 else baseline3[0]

trip_models = ["fake/model-a", "fake/model-b"]
for m in trip_models:
    for _ in range(3):
        router3.health.mark_failure(m)
check("fake models tripped before bias", all(not router3.health.is_available(m) for m in trip_models))

router3.bias_fallback_chain(target3, ttl_seconds=1800)
status3 = router3.bias_status()
check(
    "watching_recovery_of captures the currently-tripped models",
    set(status3.get("watching_recovery_of", [])) == set(trip_models),
    detail=str(status3),
)

biased_chain3 = router3._get_chain("reasoning")
check("biased model first while tripped models pending recovery", biased_chain3[0] == target3, detail=str(biased_chain3))

# Recover all watched models
for m in trip_models:
    router3.health.mark_success(m)

chain_after_recovery = router3._get_chain("reasoning")
check("bias auto-reverts once all watched models recover", router3.bias_status() == {"active": False}, detail=str(router3.bias_status()))
check("chain returns to baseline after recovery-revert", chain_after_recovery == baseline3, detail=str(chain_after_recovery))


# =============================================================================
# 3. self_healing._pick_healthy_model
# =============================================================================
print("\n== self_healing._pick_healthy_model ==")

router4 = SmartRouter()
reasoning_chain = router4._get_chain("reasoning")
to_trip = reasoning_chain[:-1] if len(reasoning_chain) > 1 else []
for m in to_trip:
    for _ in range(3):
        router4.health.mark_failure(m)

healthy = self_healing._pick_healthy_model(router4, to_trip)
check(
    "_pick_healthy_model returns an available, non-tripped model",
    healthy is not None and healthy not in to_trip,
    detail=f"healthy={healthy} to_trip={to_trip}",
)
if healthy is not None:
    check("returned model is actually available", router4.health.is_available(healthy))

# Trip every model that appears anywhere in the catalog -> no healthy pick left
router5 = SmartRouter()
all_models: set[str] = set()
for cat_data in router5.catalog["categories"].values():
    all_models.add(cat_data["primary"])
    all_models.update(cat_data.get("fallbacks", []))
for m in all_models:
    for _ in range(3):
        router5.health.mark_failure(m)

healthy5 = self_healing._pick_healthy_model(router5, list(all_models))
check("_pick_healthy_model returns None when every catalog model is tripped", healthy5 is None, detail=str(healthy5))


# =============================================================================
# 4. self_healing._log_diagnostic ring-buffer + get_diagnostics_log
# =============================================================================
print("\n== diagnostics log: ring-buffer trimming + get_diagnostics_log ==")

orig_diag_path = self_healing._DIAG_PATH
orig_max_lines = self_healing._MAX_DIAG_LINES
tmpdir1 = tempfile.mkdtemp()
try:
    tmp_diag = Path(tmpdir1) / "diagnostics.jsonl"
    self_healing._DIAG_PATH = tmp_diag
    self_healing._MAX_DIAG_LINES = 5

    for i in range(10):
        self_healing._log_diagnostic({"i": i})

    lines = tmp_diag.read_text(encoding="utf-8").splitlines()
    check("ring buffer trimmed to _MAX_DIAG_LINES", len(lines) == 5, detail=f"len={len(lines)}")
    parsed = [json.loads(l) for l in lines]
    check("ring buffer keeps the most recent entries in order", [p["i"] for p in parsed] == [5, 6, 7, 8, 9], detail=str(parsed))

    result = asyncio.run(self_healing.get_diagnostics_log(limit=2))
    check("get_diagnostics_log respects limit", result["count"] == 2, detail=str(result))
    check("get_diagnostics_log returns most recent entries", [e["i"] for e in result["entries"]] == [8, 9], detail=str(result))

    # Nonexistent file -> empty result, no crash
    self_healing._DIAG_PATH = Path(tmpdir1) / "does_not_exist.jsonl"
    result2 = asyncio.run(self_healing.get_diagnostics_log(limit=10))
    check("get_diagnostics_log handles missing file", result2 == {"entries": [], "count": 0}, detail=str(result2))
finally:
    self_healing._DIAG_PATH = orig_diag_path
    self_healing._MAX_DIAG_LINES = orig_max_lines
    shutil.rmtree(tmpdir1, ignore_errors=True)


# =============================================================================
# 5. self_healing.run_health_check() -- RAM / disk / circuit-breaker
#    remediation with anti-thrashing, driven through mocked collaborators.
# =============================================================================
print("\n== run_health_check: RAM / disk / circuit-breaker remediation ==")


class FakeHealth:
    def __init__(self, tripped_models):
        self._tripped_models = list(tripped_models)

    def get_status_summary(self):
        return {
            "tripped_models": list(self._tripped_models),
            "tripped_detail": [{"model": m, "cooldown_remaining_s": 60} for m in self._tripped_models],
            "trip_counts": {m: 3 for m in self._tripped_models},
            "threshold": 3,
            "cooldown_seconds": 120,
        }


class FakeRouter:
    def __init__(self, tripped_models):
        self.health = FakeHealth(tripped_models)
        self.bias_calls: list[str] = []

    def bias_fallback_chain(self, prefer, ttl_seconds=1800):
        self.bias_calls.append(prefer)
        return {"biased_model": prefer, "ttl_seconds": ttl_seconds, "watching_recovery_of": []}


def make_resource_stats(ram: float, disk: float, cpu: float = 10.0):
    async def _stats():
        return {"cpu_percent": cpu, "ram_used_pct": ram, "disk_used_pct": disk}
    return _stats


_clear_calls = {"n": 0}


def fake_clear_memory_caches():
    _clear_calls["n"] += 1
    return ["FAKE_RAM_ACTION"]


_purge_calls = {"n": 0}


async def fake_purge_old_data():
    _purge_calls["n"] += 1
    return ["FAKE_DISK_ACTION"]


orig_get_resource_stats = self_healing._get_resource_stats
orig_clear_memory_caches = self_healing._clear_memory_caches
orig_purge_old_data = self_healing._purge_old_data
orig_pick_healthy_model = self_healing._pick_healthy_model
orig_diag_path2 = self_healing._DIAG_PATH
orig_get_router = router_mod.get_router
orig_state = dict(self_healing._state)

settings = get_settings()
tmpdir2 = tempfile.mkdtemp()

try:
    diag_path2 = Path(tmpdir2) / "diagnostics.jsonl"
    self_healing._DIAG_PATH = diag_path2
    self_healing._clear_memory_caches = fake_clear_memory_caches
    self_healing._purge_old_data = fake_purge_old_data
    self_healing._pick_healthy_model = lambda router, tripped: "fake/healthy-model"
    router_mod.get_router = lambda: FakeRouter(tripped_models=[])
    self_healing._state["last_ram_action_pct"] = None
    self_healing._state["last_disk_action_pct"] = None
    self_healing._state["last_bias_model"] = None

    # -- A: RAM high, first run -> cache clear ------------------------------
    self_healing._get_resource_stats = make_resource_stats(ram=settings.ram_threshold_pct + 5, disk=50.0)
    entry = asyncio.run(self_healing.run_health_check())
    check("RAM-high first run triggers cache clear", "FAKE_RAM_ACTION" in entry["actions"], detail=str(entry["actions"]))
    check("_clear_memory_caches called once", _clear_calls["n"] == 1)
    check("last_ram_action_pct recorded", self_healing._state["last_ram_action_pct"] == settings.ram_threshold_pct + 5)
    check("no extraneous actions when only RAM is high", entry["actions"] == ["FAKE_RAM_ACTION"], detail=str(entry["actions"]))

    # -- B: RAM still high, no improvement -> condition persists ------------
    self_healing._get_resource_stats = make_resource_stats(ram=settings.ram_threshold_pct + 7, disk=50.0)
    entry = asyncio.run(self_healing.run_health_check())
    check("RAM still high -> 'condition persists' message", any("condition persists" in a for a in entry["actions"]), detail=str(entry["actions"]))
    check("_clear_memory_caches NOT called again (anti-thrash)", _clear_calls["n"] == 1)
    check("last_ram_action_pct updated", self_healing._state["last_ram_action_pct"] == settings.ram_threshold_pct + 7)

    # -- C: RAM back to normal -> no action, state resets --------------------
    self_healing._get_resource_stats = make_resource_stats(ram=settings.ram_threshold_pct - 10, disk=50.0)
    entry = asyncio.run(self_healing.run_health_check())
    check("RAM normal -> no actions", entry["actions"] == [], detail=str(entry["actions"]))
    check("last_ram_action_pct reset to None", self_healing._state["last_ram_action_pct"] is None)

    # -- D: Disk high, first run -> purge -------------------------------------
    self_healing._get_resource_stats = make_resource_stats(ram=50.0, disk=settings.disk_threshold_pct + 2)
    entry = asyncio.run(self_healing.run_health_check())
    check("disk-high first run triggers purge", "FAKE_DISK_ACTION" in entry["actions"], detail=str(entry["actions"]))
    check("_purge_old_data called once", _purge_calls["n"] == 1)
    check("last_disk_action_pct recorded", self_healing._state["last_disk_action_pct"] == settings.disk_threshold_pct + 2)

    # -- E: Disk still high, no improvement -> condition persists ------------
    self_healing._get_resource_stats = make_resource_stats(ram=50.0, disk=settings.disk_threshold_pct + 5)
    entry = asyncio.run(self_healing.run_health_check())
    check("disk still high -> 'condition persists' message", any("condition persists" in a for a in entry["actions"]), detail=str(entry["actions"]))
    check("_purge_old_data NOT called again (anti-thrash)", _purge_calls["n"] == 1)

    # -- F: Disk back to normal -> no action, state resets --------------------
    self_healing._get_resource_stats = make_resource_stats(ram=50.0, disk=settings.disk_threshold_pct - 20)
    entry = asyncio.run(self_healing.run_health_check())
    check("disk normal -> no actions", entry["actions"] == [], detail=str(entry["actions"]))
    check("last_disk_action_pct reset to None", self_healing._state["last_disk_action_pct"] is None)

    # -- G: circuit-trip >= threshold, first run -> bias toward healthy model
    self_healing._get_resource_stats = make_resource_stats(ram=50.0, disk=50.0)
    fake_router = FakeRouter(tripped_models=["m1", "m2", "m3"])
    router_mod.get_router = lambda: fake_router
    entry = asyncio.run(self_healing.run_health_check())
    check(
        "circuit-trip first run biases toward healthy model",
        any("biased fallback chains toward fake/healthy-model" in a for a in entry["actions"]),
        detail=str(entry["actions"]),
    )
    check("bias_fallback_chain called once with the healthy model", fake_router.bias_calls == ["fake/healthy-model"], detail=str(fake_router.bias_calls))
    check("last_bias_model recorded", self_healing._state["last_bias_model"] == "fake/healthy-model")

    # -- H: still tripped, same healthy pick -> condition persists, no repeat
    entry = asyncio.run(self_healing.run_health_check())
    check(
        "repeat circuit-trip -> bias already active / condition persists",
        any("already active" in a and "condition persists" in a for a in entry["actions"]),
        detail=str(entry["actions"]),
    )
    check("bias_fallback_chain NOT called again (anti-thrash)", fake_router.bias_calls == ["fake/healthy-model"], detail=str(fake_router.bias_calls))

    # -- I: recovered -> no bias action, state cleared ------------------------
    fake_router_recovered = FakeRouter(tripped_models=[])
    router_mod.get_router = lambda: fake_router_recovered
    entry = asyncio.run(self_healing.run_health_check())
    check("recovered -> no bias-related action", not any("bias" in a.lower() for a in entry["actions"]), detail=str(entry["actions"]))
    check("last_bias_model cleared", self_healing._state["last_bias_model"] is None)

    # -- J: tripped >= threshold but no healthy alternative -------------------
    self_healing._pick_healthy_model = lambda router, tripped: None
    fake_router_none = FakeRouter(tripped_models=["m1", "m2", "m3"])
    router_mod.get_router = lambda: fake_router_none
    entry = asyncio.run(self_healing.run_health_check())
    check(
        "no healthy alternative -> explanatory action",
        any("no healthy alternative found" in a for a in entry["actions"]),
        detail=str(entry["actions"]),
    )
    check("bias_fallback_chain not called when no healthy alt exists", fake_router_none.bias_calls == [])

    # -- Diagnostics file should have one JSON line per run (A-J = 10 runs) --
    diag_lines = diag_path2.read_text(encoding="utf-8").splitlines()
    check("diagnostics.jsonl has one line per run", len(diag_lines) == 10, detail=f"len={len(diag_lines)}")
    last_entry = json.loads(diag_lines[-1])
    check("each diagnostics entry carries resources/router_health/actions/thresholds", set(["ts", "resources", "router_health", "actions", "thresholds"]).issubset(last_entry.keys()), detail=str(last_entry.keys()))

finally:
    self_healing._get_resource_stats = orig_get_resource_stats
    self_healing._clear_memory_caches = orig_clear_memory_caches
    self_healing._purge_old_data = orig_purge_old_data
    self_healing._pick_healthy_model = orig_pick_healthy_model
    self_healing._DIAG_PATH = orig_diag_path2
    router_mod.get_router = orig_get_router
    self_healing._state.clear()
    self_healing._state.update(orig_state)
    shutil.rmtree(tmpdir2, ignore_errors=True)


# =============================================================================
# 6. ChromaManager.delete_old_conversations -- only touches 'conversations'
# =============================================================================
print("\n== ChromaManager.delete_old_conversations ==")


class FakeCollection:
    """Minimal stand-in for a chromadb Collection."""

    def __init__(self, items):
        self.items = list(items)  # [{"id": ..., "metadata": {"timestamp": ...}}, ...]
        self.deleted_ids: list[str] = []
        self.get_calls: list[dict] = []

    def get(self, where=None):
        self.get_calls.append(where)
        cutoff = where["timestamp"]["$lt"]
        ids = [it["id"] for it in self.items if it["metadata"]["timestamp"] < cutoff]
        return {"ids": ids}

    def delete(self, ids):
        self.deleted_ids.extend(ids)
        self.items = [it for it in self.items if it["id"] not in ids]


class FailingCollection:
    """Simulates a ChromaDB query error -- delete_old_conversations must not raise."""

    def get(self, where=None):
        raise RuntimeError("simulated chroma query failure")

    def delete(self, ids):
        raise AssertionError("delete() should not be called when get() failed")


now = time.time()
old_ts = now - 40 * 86400  # 40 days old -- past the 30-day cutoff
new_ts = now - 5 * 86400   # 5 days old -- within the cutoff

# -- Mixed old/new entries: only old ones removed ----------------------------
mgr = chroma_mod.ChromaManager.__new__(chroma_mod.ChromaManager)
mgr._lock = asyncio.Lock()
fake_col = FakeCollection([
    {"id": "old1", "metadata": {"timestamp": old_ts}},
    {"id": "old2", "metadata": {"timestamp": old_ts}},
    {"id": "new1", "metadata": {"timestamp": new_ts}},
])
mgr._cols = {chroma_mod.COL_CONVERSATIONS: fake_col}

removed = asyncio.run(mgr.delete_old_conversations(days=30))
check("delete_old_conversations removes only entries older than cutoff", removed == 2, detail=str(removed))
check("old entries deleted from collection", set(fake_col.deleted_ids) == {"old1", "old2"}, detail=str(fake_col.deleted_ids))
check("recent entry retained", any(it["id"] == "new1" for it in fake_col.items), detail=str(fake_col.items))
check("only the conversations collection was touched", set(mgr._cols.keys()) == {chroma_mod.COL_CONVERSATIONS}, detail=str(mgr._cols.keys()))

# -- Nothing old: 0 removed, delete() not called ------------------------------
mgr2 = chroma_mod.ChromaManager.__new__(chroma_mod.ChromaManager)
mgr2._lock = asyncio.Lock()
fake_col2 = FakeCollection([{"id": "new1", "metadata": {"timestamp": new_ts}}])
mgr2._cols = {chroma_mod.COL_CONVERSATIONS: fake_col2}

removed2 = asyncio.run(mgr2.delete_old_conversations(days=30))
check("no old entries -> 0 removed", removed2 == 0, detail=str(removed2))
check("delete() not called when nothing to remove", fake_col2.deleted_ids == [], detail=str(fake_col2.deleted_ids))

# -- Query failure: returns 0 gracefully, never raises ------------------------
mgr3 = chroma_mod.ChromaManager.__new__(chroma_mod.ChromaManager)
mgr3._lock = asyncio.Lock()
mgr3._cols = {chroma_mod.COL_CONVERSATIONS: FailingCollection()}

removed3 = asyncio.run(mgr3.delete_old_conversations(days=30))
check("query failure -> returns 0 without raising", removed3 == 0, detail=str(removed3))


# =============================================================================
# 7. config.py Phase 28 settings
# =============================================================================
print("\n== config.py Phase 28 settings ==")

s = get_settings()
check("self_healing_enabled default True", s.self_healing_enabled is True)
check("self_healing_interval_sec default 300", s.self_healing_interval_sec == 300)
check("ram_threshold_pct default 85.0", s.ram_threshold_pct == 85.0)
check("disk_threshold_pct default 90.0", s.disk_threshold_pct == 90.0)
check("circuit_trip_reorder_threshold default 3", s.circuit_trip_reorder_threshold == 3)


# =============================================================================
# 8. system_health.py route registration
# =============================================================================
print("\n== system_health API router registration ==")

try:
    from src.api.system_health import router as health_router  # noqa: E402

    paths = {route.path for route in health_router.routes}
    check("status route registered", "/api/system/health/status" in paths, detail=str(paths))
    check("diagnostics route registered", "/api/system/health/diagnostics" in paths, detail=str(paths))
    check("check route registered", "/api/system/health/check" in paths, detail=str(paths))
except ModuleNotFoundError as e:
    print(f"  SKIP  system_health route registration (fastapi not installed: {e})")


# =============================================================================
# Summary
# =============================================================================
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
