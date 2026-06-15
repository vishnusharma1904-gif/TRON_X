"""
TRON-X Security Findings & Reporting
------------------------------------
A single normalized Finding schema that every recon/scanner module emits, plus
helpers to render a report as JSON and Markdown.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

Severity = Literal["info", "low", "medium", "high", "critical"]

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_REPORT_DIR = Path(__file__).resolve().parents[2] / "reports" / "sec"


@dataclass
class Finding:
    target: str
    tier: int
    severity: Severity
    title: str
    tool: str
    evidence: str = ""
    remediation: str = ""
    references: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    engagement: str
    target: str
    started: str
    findings: list[Finding] = field(default_factory=list)

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in self.findings:
            c[f.severity] = c.get(f.severity, 0) + 1
        return c

    def to_json(self) -> str:
        return json.dumps(
            {
                "engagement": self.engagement,
                "target": self.target,
                "started": self.started,
                "counts": self.counts(),
                "findings": [f.to_dict() for f in self.sorted_findings()],
            },
            indent=2,
        )

    def to_markdown(self, summary: str = "") -> str:
        lines = [
            f"# Security Report — {self.target}",
            "",
            f"- **Engagement:** {self.engagement}",
            f"- **Started:** {self.started}",
            f"- **Findings:** {', '.join(f'{k}={v}' for k, v in self.counts().items()) or 'none'}",
            "",
        ]
        if summary:
            lines += ["## Summary", "", summary, ""]
        lines += ["## Findings", ""]
        if not self.findings:
            lines.append("_No findings recorded._")
        for f in self.sorted_findings():
            lines += [
                f"### [{f.severity.upper()}] {f.title}",
                f"- **Tool:** {f.tool}  |  **Tier:** {f.tier}",
            ]
            if f.evidence:
                lines.append(f"- **Evidence:** {f.evidence}")
            if f.remediation:
                lines.append(f"- **Remediation:** {f.remediation}")
            if f.references:
                lines.append(f"- **References:** {', '.join(f.references)}")
            lines.append("")
        return "\n".join(lines)

    def save(self, summary: str = "") -> dict[str, str]:
        """Write .md and .json to reports/sec/. Returns the paths."""
        _REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = f"{self.engagement or 'adhoc'}-{ts}"
        md_path = _REPORT_DIR / f"{stem}.md"
        json_path = _REPORT_DIR / f"{stem}.json"
        md_path.write_text(self.to_markdown(summary), encoding="utf-8")
        json_path.write_text(self.to_json(), encoding="utf-8")
        return {"markdown": str(md_path), "json": str(json_path)}


def new_report(engagement: str, target: str) -> Report:
    return Report(
        engagement=engagement or "adhoc",
        target=target,
        started=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
