"""
TRON-X Security Agent  (authorized recon & vulnerability scanning)
------------------------------------------------------------------
JARVIS/FRIDAY-style natural-language entrypoint for ethical, scope-gated
penetration testing of systems you own or are authorized to test.

Every active operation passes through the scope gate (src/security/scope.py)
and is recorded in the audit log (src/security/audit.py). The persona/LLM
layer cannot override the gate — it is enforced in code here.

Usage:
    agent = SecurityAgent()
    result = await agent.run("recon example.com")
    result = await agent.run("scan 192.168.1.10 ports 1-1024", engagement_id="homelab")
"""
from __future__ import annotations

import re
from typing import Optional

from src.core.logger import log
from src.security.scope import load_scope
from src.security.audit import audit
from src.security import recon as _recon
from src.security import scanners as _scan
from src.security.report import Finding, new_report

# Maps a parsed action to the tier it requires.
_TIER = {"recon": 0, "headers": 0, "scan": 1, "nmap": 1, "nuclei": 1, "tls": 1}


class SecurityAgent:
    """Scope-gated orchestrator over recon + active scanners."""

    async def run(self, request: str, *, engagement_id: Optional[str] = None,
                  persona: str = "jarvis", session_id: str = "__sec__") -> dict:
        action, target, opts = self._parse(request)
        if not target:
            return {"ok": False, "reply":
                    "No target found. Try: 'recon example.com' or "
                    "'scan 192.168.1.10 ports 1-1024'."}

        tier = _TIER.get(action, 0)

        # ---- Authorization gate (hard, non-overridable) -------------------
        scope = load_scope()
        decision = scope.check(target, tier)
        audit(action, target=target, tier=tier, allowed=decision.allowed,
              engagement=decision.engagement_id or engagement_id,
              reason=decision.reason)
        if not decision.allowed:
            log.warning("[security] DENIED %s %s (tier %d): %s",
                        action, target, tier, decision.reason)
            return {"ok": False, "denied": True, "reply": decision.reason}

        # ---- Run the requested operation ----------------------------------
        report = new_report(decision.engagement_id or "adhoc", target)
        try:
            report.findings = await self._dispatch(action, target, opts)
        except Exception as ex:  # noqa: BLE001
            log.error("[security] operation error: %s", ex)
            return {"ok": False, "reply": f"Operation failed: {ex}"}

        summary = await self._summarize(report, persona, session_id)
        paths = report.save(summary)
        audit(action + ":done", target=target, tier=tier, allowed=True,
              engagement=decision.engagement_id,
              reason=f"{len(report.findings)} findings",
              extra={"report": paths})

        return {
            "ok": True,
            "engagement": decision.engagement_id,
            "target": target,
            "tier": tier,
            "counts": report.counts(),
            "findings": [f.to_dict() for f in report.sorted_findings()],
            "report_paths": paths,
            "reply": summary,
        }

    # ------------------------------------------------------------------ #
    async def _dispatch(self, action: str, target: str, opts: dict) -> list[Finding]:
        if action in ("recon", "headers"):
            if action == "headers" or target.startswith("http") or "/" in opts.get("raw", ""):
                return await _recon.recon_url(target)
            return await _recon.recon_host(target)
        if action == "nmap":
            return await _scan.nmap_scan(target, ports=opts.get("ports", "1-1024"))
        if action == "nuclei":
            return await _scan.nuclei_scan(target)
        if action == "tls":
            return await _scan.testssl_scan(target)
        if action == "scan":
            # full Tier-1 sweep: nmap + nuclei + testssl + passive recon
            findings: list[Finding] = []
            findings += await _recon.recon_url(target) if ("." in target) else []
            findings += await _scan.nmap_scan(target, ports=opts.get("ports", "1-1024"))
            findings += await _scan.nuclei_scan(target)
            findings += await _scan.testssl_scan(_host_only(target))
            return findings
        return await _recon.recon_host(target)

    # ------------------------------------------------------------------ #
    def _parse(self, request: str) -> tuple[str, str, dict]:
        """Very small NL parser: action keyword + first host/URL token + ports."""
        text = request.strip()
        low = text.lower()

        action = "recon"
        if re.search(r"\bnmap|open ports?|port scan\b", low):
            action = "nmap"
        elif "nuclei" in low or "web vuln" in low:
            action = "nuclei"
        elif "testssl" in low or ("tls" in low and "scan" in low) or "ssl scan" in low:
            action = "tls"
        elif re.search(r"\bfull scan|pentest|vuln(?:erability)? scan|\bscan\b", low):
            action = "scan"
        elif "header" in low:
            action = "headers"
        elif "recon" in low:
            action = "recon"

        target = _extract_target(text)
        ports_m = re.search(r"ports?\s+([0-9,\-]+)", low)
        opts = {"raw": text}
        if ports_m:
            opts["ports"] = ports_m.group(1)
        return action, target, opts

    # ------------------------------------------------------------------ #
    async def _summarize(self, report, persona: str, session_id: str) -> str:
        """Use the existing orchestrator to write an executive summary."""
        if not report.findings:
            return "Scan complete — no findings recorded."
        try:
            from src.intelligence.orchestrator import get_orchestrator
            orch = get_orchestrator()
            bullet = "\n".join(
                f"- [{f.severity}] {f.title} ({f.tool}): {f.evidence}"
                for f in report.sorted_findings()[:40]
            )
            prompt = (
                f"You are a security analyst. Summarize these findings for "
                f"target {report.target} in 4-8 sentences: highlight the most "
                f"important issues and concrete next steps.\n\n{bullet}"
            )
            res = await orch.chat(user_message=prompt, session_id=session_id,
                                  intent="security", persona=persona, max_tokens=500)
            return res.get("reply") or _fallback_summary(report)
        except Exception as ex:  # noqa: BLE001
            log.warning("[security] LLM summary failed (%s); using fallback", ex)
            return _fallback_summary(report)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_HOST_RE = re.compile(
    r"(https?://[^\s]+)"                              # URL
    r"|((?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?)"        # IPv4 / CIDR
    r"|([a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9\-]+)+)"  # hostname
)


def _extract_target(text: str) -> str:
    m = _HOST_RE.search(text)
    if not m:
        return ""
    return next(g for g in m.groups() if g)


def _host_only(target: str) -> str:
    t = target
    if "://" in t:
        t = t.split("://", 1)[1]
    return t.split("/", 1)[0].split(":", 1)[0]


def _fallback_summary(report) -> str:
    c = report.counts()
    parts = ", ".join(f"{k}: {v}" for k, v in c.items())
    return f"Scan of {report.target} complete. Findings by severity — {parts}."
