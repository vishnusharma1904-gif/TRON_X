# TRON-X SecurityAgent — Design Proposal

**Status:** Proposal (no code changed yet)
**Author:** generated for Vishnu
**Scope:** Add a JARVIS/FRIDAY-style *ethical hacking & penetration testing* capability to TRON-X.

---

## 0. Ground rules (read first)

This agent is designed for **authorized, defensive security work only** — testing
systems *you own* or have **written permission** to test. It is built as an
**orchestration layer over standard, well-known security tools** (nmap, nuclei,
OWASP ZAP, etc.). It deliberately does **not** ship custom exploit code, malware,
C2, or payload generators.

Two hard gates are baked into the design so the assistant can't be casually turned
into an attack tool:

1. **Authorization gate** — every scan must reference an authorized target from a
   scope allowlist (`config/pentest_scope.yaml`). Targets not on the list are refused.
2. **Action tiers** — passive recon (always allowed) → active scanning (requires
   scope match) → exploitation/intrusion (off by default, requires an explicit
   `--i-have-written-authorization` flag + a logged engagement ID).

Everything is audit-logged. This keeps the feature genuinely useful for securing
your own stack while staying on the right side of the line.

---

## 1. How it fits the existing architecture

TRON-X already has a clean agent pattern:

- Agents live in `src/agents/*.py` as classes with an async `run()`.
- The `TaskCoordinator` (`src/agents/coordinator.py`) exposes a `@register_agent`
  registry that the HUD/agent palette can call.
- API routers live in `src/api/*.py` and mount under the FastAPI app.
- Config flows through `src/core/config.py` (settings) + `config/*.yaml|json`.
- Auth/rate-limit middleware already exists (`src/core/auth.py`, `ratelimit.py`).

The SecurityAgent follows the same conventions exactly — no new patterns.

```
src/agents/security_agent.py        # the agent (orchestrator + tiers)
src/security/__init__.py
src/security/scope.py               # authorization allowlist loader + matcher
src/security/tools.py               # thin wrappers around external CLI tools
src/security/recon.py               # passive recon (DNS, whois, headers, TLS)
src/security/scanners.py            # active scanning (nmap, nuclei, zap)
src/security/report.py              # findings -> markdown/JSON report
src/security/audit.py               # append-only engagement audit log
src/api/security.py                 # FastAPI routes
config/pentest_scope.yaml           # the ONLY place authorized targets are defined
```

---

## 2. Capability tiers

### Tier 0 — Passive recon (safe, always on)
No packets sent to the target beyond normal lookups.
- DNS / reverse DNS, WHOIS, subdomain enumeration (passive sources)
- TLS certificate inspection, security-header analysis of a URL
- Tech fingerprinting from public responses
- CVE lookup for identified versions (read-only feeds)

### Tier 1 — Active scanning (requires scope match)
Touches the target but is non-destructive.
- Port/service discovery — `nmap` (`-sV`, no aggressive scripts by default)
- Web vuln scanning — `nuclei` with community templates
- TLS/SSL config audit — `testssl.sh`
- Auth'd web app scan — OWASP **ZAP** baseline/spider (passive + safe active)

### Tier 2 — Exploitation / validation (OFF by default)
Disabled unless an engagement is explicitly opened with written-authorization
attestation. Even then, scoped to *validation* of findings (e.g. confirming a
known CVE is reachable) rather than weaponization. No payload/malware generation —
the agent refuses requests to author offensive code and instead points to running
the recognized tool against an in-scope, authorized target.

---

## 3. The authorization gate

`config/pentest_scope.yaml`:

```yaml
engagements:
  - id: "homelab-2026"
    owner: "Vishnu"
    authorized_by: "self (asset owner)"
    expires: "2026-12-31"
    max_tier: 2
    targets:
      - "192.168.1.0/24"
      - "*.lab.tron-x.local"
  - id: "myapp-prod"
    owner: "Vishnu"
    authorized_by: "self (asset owner)"
    expires: "2026-09-30"
    max_tier: 1
    targets:
      - "tron-x.example.com"
```

`scope.py` resolves a requested target+tier against this file. Refusals are
explicit: *"target X is not in any active engagement; add it to
config/pentest_scope.yaml with your authorization to proceed."* Expired engagements
auto-deny. The LLM/persona layer never overrides the scope check — it's a hard
gate in code, not a prompt instruction.

---

## 4. Agent interface

```python
class SecurityAgent:
    async def run(self, request: str, *, engagement_id: str | None = None,
                  persona: str = "jarvis", session_id: str = "__sec__") -> dict:
        """
        Natural-language entrypoint, e.g.:
          "scan my homelab for open ports"
          "check tron-x.example.com security headers and TLS"
          "run a nuclei web scan on the myapp engagement"
        Pipeline: parse intent -> resolve target & tier -> scope check
                  -> run tool(s) -> normalize findings -> LLM summary -> report
        """
```

Registry hook (so it shows in the agent palette / coordinator):

```python
@register_agent("security_scan",
                "Authorized recon & vuln scanning (scope-gated pentest)")
async def _agent_security(payload: dict) -> dict:
    from src.agents.security_agent import SecurityAgent
    return await SecurityAgent().run(payload.get("query", payload.get("input", "")),
                                     engagement_id=payload.get("engagement_id"))
```

---

## 5. API surface (`src/api/security.py`)

| Method | Path | Purpose |
|---|---|---|
| GET  | `/security/engagements` | List active authorized engagements |
| POST | `/security/scope/check` | Dry-run: is target X allowed at tier N? |
| POST | `/security/recon` | Tier 0 passive recon on a URL/host |
| POST | `/security/scan` | Tier 1 active scan (scope-gated) |
| GET  | `/security/report/{id}` | Fetch a finished engagement report |
| GET  | `/security/audit` | View the audit log |

All routes sit behind the existing `require_api_key` dependency and rate limiter.

---

## 6. Reporting

`report.py` normalizes every tool's output into a common finding schema:

```json
{
  "target": "tron-x.example.com",
  "tier": 1,
  "severity": "medium",
  "title": "Missing Content-Security-Policy header",
  "evidence": "...",
  "remediation": "Add a restrictive CSP...",
  "references": ["CWE-693"],
  "tool": "header-audit"
}
```

The LLM (via your existing orchestrator) writes an executive summary; raw findings
stay structured for the HUD. Reports save to `reports/sec/<engagement>-<ts>.md|json`.

---

## 7. Safeguards summary

- Hard-coded scope allowlist; expired/missing → refuse.
- Tier 2 disabled unless engagement opened with written-auth attestation.
- No exploit/malware/payload authoring — agent declines and redirects to running
  recognized tools against authorized targets.
- Append-only audit log (`audit.py`): who, what target, which tier, when, result.
- Rate-limited + API-key protected like every other route.
- Dependencies (nmap, nuclei, zap, testssl.sh) are external binaries the user
  installs deliberately; nothing bundled.

---

## 8. New dependencies

Mostly external CLI tools (documented in README, not pip):
`nmap`, `nuclei`, `testssl.sh`, OWASP `zaproxy`.
Python side (light): `python-nmap` (parse nmap XML), `dnspython`, `cryptography`
(TLS cert parsing) — all mainstream, already-style-compatible with your stack.

---

## 9. Suggested build order

1. `config/pentest_scope.yaml` + `src/security/scope.py` + `audit.py` (the gates).
2. Tier 0 `recon.py` (no external tools needed — pure Python/httpx).
3. `src/api/security.py` with `/recon` + `/scope/check`.
4. `SecurityAgent` wrapping the above + coordinator registration.
5. Tier 1 `scanners.py` (nmap/nuclei) once tools are installed.
6. `report.py` + HUD wiring.
7. Tier 2 left stubbed/disabled until you explicitly want it.

This sequences value early (recon + header/TLS audit work day one with zero extra
binaries) and defers the heavier, more sensitive pieces.

---

**Next step:** tell me to proceed and I'll start at step 1, or adjust scope/tiers
first. I'd recommend we begin with Tier 0 + the scope gate so you have a working,
safe baseline before adding active scanners.
