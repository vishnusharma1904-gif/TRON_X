"""
TRON-X Penetration-Test Scope Gate
----------------------------------
Hard, code-level authorization check. Every active operation must resolve a
target to an *active, non-expired* engagement in config/pentest_scope.yaml at
a tier <= that engagement's max_tier. This is NOT a prompt instruction — the
LLM/persona layer cannot override it.

Public API:
    load_scope() -> Scope
    Scope.check(target, tier) -> ScopeDecision
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from src.core.logger import log

# config/pentest_scope.yaml relative to repo root (this file is src/security/)
_SCOPE_PATH = Path(__file__).resolve().parents[2] / "config" / "pentest_scope.yaml"


@dataclass
class Engagement:
    id: str
    owner: str
    authorized_by: str
    expires: Optional[_dt.date]
    max_tier: int
    targets: list[str] = field(default_factory=list)

    def is_active(self, on: Optional[_dt.date] = None) -> bool:
        on = on or _dt.date.today()
        return self.expires is None or self.expires >= on


@dataclass
class ScopeDecision:
    allowed: bool
    reason: str
    engagement_id: Optional[str] = None
    matched_target: Optional[str] = None


def _parse_date(v) -> Optional[_dt.date]:
    if v in (None, "", "never"):
        return None
    if isinstance(v, _dt.date):
        return v
    return _dt.date.fromisoformat(str(v))


def _host_in_pattern(target: str, pattern: str) -> bool:
    """Match a target host/IP against a scope pattern (CIDR, glob, or exact)."""
    target = target.strip().lower()
    pattern = pattern.strip().lower()

    # CIDR / IP-range match
    if "/" in pattern:
        try:
            net = ipaddress.ip_network(pattern, strict=False)
            return ipaddress.ip_address(target) in net
        except ValueError:
            return False

    # Exact IP equality
    try:
        return ipaddress.ip_address(target) == ipaddress.ip_address(pattern)
    except ValueError:
        pass

    # Hostname glob (supports *.example.com and exact)
    return fnmatch.fnmatch(target, pattern)


@dataclass
class Scope:
    engagements: list[Engagement]

    def check(self, target: str, tier: int) -> ScopeDecision:
        """Return an explicit allow/deny decision for target+tier."""
        if not target:
            return ScopeDecision(False, "No target specified.")

        host = _normalize_target(target)
        any_target_match = False

        for eng in self.engagements:
            for pat in eng.targets:
                if _host_in_pattern(host, pat):
                    any_target_match = True
                    if not eng.is_active():
                        return ScopeDecision(
                            False,
                            f"Engagement '{eng.id}' covering {host} expired on "
                            f"{eng.expires}. Renew it in config/pentest_scope.yaml.",
                            eng.id, pat,
                        )
                    if tier > eng.max_tier:
                        return ScopeDecision(
                            False,
                            f"Tier {tier} exceeds max_tier {eng.max_tier} for "
                            f"engagement '{eng.id}'. Raise max_tier to authorize.",
                            eng.id, pat,
                        )
                    return ScopeDecision(
                        True,
                        f"Authorized under engagement '{eng.id}' "
                        f"(owner={eng.owner}, by={eng.authorized_by}).",
                        eng.id, pat,
                    )

        if any_target_match:
            # matched but every match was denied above; shouldn't reach here
            return ScopeDecision(False, f"{host} is not authorized at tier {tier}.")

        return ScopeDecision(
            False,
            f"Target '{host}' is not in any engagement. Add it to "
            f"config/pentest_scope.yaml with your authorization to proceed.",
        )


def _normalize_target(target: str) -> str:
    """Strip scheme/port/path so 'https://host:8443/x' -> 'host'."""
    t = target.strip()
    if "://" in t:
        t = t.split("://", 1)[1]
    t = t.split("/", 1)[0]
    # strip :port but keep IPv6 in brackets intact
    if t.startswith("["):
        return t  # IPv6 literal, leave as-is
    if t.count(":") == 1:
        t = t.split(":", 1)[0]
    return t.lower()


def load_scope(path: Path | None = None) -> Scope:
    """Load and parse the scope file. Returns an empty scope if missing."""
    p = path or _SCOPE_PATH
    if not p.exists():
        log.warning("[security] scope file not found: %s — all targets will be denied", p)
        return Scope(engagements=[])

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    engs: list[Engagement] = []
    for e in raw.get("engagements", []) or []:
        try:
            engs.append(Engagement(
                id=str(e["id"]),
                owner=str(e.get("owner", "")),
                authorized_by=str(e.get("authorized_by", "")),
                expires=_parse_date(e.get("expires")),
                max_tier=int(e.get("max_tier", 0)),
                targets=[str(t) for t in (e.get("targets") or [])],
            ))
        except Exception as ex:  # noqa: BLE001
            log.error("[security] skipping malformed engagement %r: %s", e.get("id"), ex)
    log.info("[security] loaded %d engagement(s) from scope", len(engs))
    return Scope(engagements=engs)
