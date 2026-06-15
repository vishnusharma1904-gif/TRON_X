"""
TRON-X Tier 1 — Active Scanners
-------------------------------
Thin async wrappers around standard, externally-installed security tools:
    nmap        service/version detection
    nuclei      template-based web vulnerability scanning
    testssl.sh  TLS/SSL configuration audit

Design notes:
  * Every wrapper checks the binary exists; if missing it returns a single
    'info' finding telling the user how to install it (no crash).
  * Flags are conservative/non-destructive by default (e.g. nmap -sV, no -A,
    no NSE 'intrusive' scripts; nuclei excludes 'dos' templates).
  * These functions assume the caller has ALREADY passed the scope gate.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import xml.etree.ElementTree as ET
from typing import Optional

from src.core.logger import log
from src.security.report import Finding

# Hard ceiling so a scan can't run forever / hang the assistant.
_DEFAULT_TIMEOUT = 600  # seconds


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _missing_tool_finding(target: str, tool: str, how: str) -> Finding:
    return Finding(
        target=target, tier=1, severity="info",
        title=f"Scanner '{tool}' not installed",
        tool=tool, evidence="binary not found on PATH",
        remediation=how,
    )


async def _run(cmd: list[str], timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run a subprocess, return (returncode, stdout, stderr)."""
    log.info("[security] exec: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", f"timed out after {timeout}s"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


# ---------------------------------------------------------------------------
# nmap
# ---------------------------------------------------------------------------
async def nmap_scan(target: str, ports: str = "1-1024",
                    timeout: int = _DEFAULT_TIMEOUT) -> list[Finding]:
    if not _have("nmap"):
        return [_missing_tool_finding(
            target, "nmap",
            "Install nmap: Windows -> https://nmap.org/download.html ; "
            "Debian/Ubuntu -> 'sudo apt install nmap'.")]

    # -sV service detection, -T4 timing, -Pn skip host-discovery, XML to stdout.
    cmd = ["nmap", "-sV", "-T4", "-Pn", "-p", ports, "-oX", "-", target]
    rc, out, err = await _run(cmd, timeout)
    if rc not in (0,) or not out.strip():
        return [Finding(target=target, tier=1, severity="info",
                        title="nmap scan produced no parseable output",
                        tool="nmap", evidence=(err or out)[:400])]
    return _parse_nmap_xml(out, target)


def _parse_nmap_xml(xml_text: str, target: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as ex:
        return [Finding(target=target, tier=1, severity="info",
                        title="nmap XML parse error", tool="nmap",
                        evidence=str(ex))]
    for host in root.findall("host"):
        for port in host.findall("./ports/port"):
            state = port.find("state")
            if state is None or state.get("state") != "open":
                continue
            portid = port.get("portid", "?")
            proto = port.get("protocol", "tcp")
            svc = port.find("service")
            name = svc.get("name", "") if svc is not None else ""
            product = svc.get("product", "") if svc is not None else ""
            version = svc.get("version", "") if svc is not None else ""
            banner = " ".join(x for x in (product, version) if x).strip()
            out.append(Finding(
                target=target, tier=1, severity="info",
                title=f"Open port {portid}/{proto} ({name or 'unknown'})",
                tool="nmap",
                evidence=f"service={name} {banner}".strip(),
                remediation="Close the port or firewall it if the service is "
                            "not intentionally exposed.",
            ))
    if not out:
        out.append(Finding(target=target, tier=1, severity="info",
                           title="No open ports found in scanned range",
                           tool="nmap", evidence=""))
    return out


# ---------------------------------------------------------------------------
# nuclei
# ---------------------------------------------------------------------------
async def nuclei_scan(url: str, timeout: int = _DEFAULT_TIMEOUT) -> list[Finding]:
    if not _have("nuclei"):
        return [_missing_tool_finding(
            url, "nuclei",
            "Install nuclei from https://github.com/projectdiscovery/nuclei "
            "and run 'nuclei -update-templates' once.")]
    if "://" not in url:
        url = "https://" + url

    # JSONL output; exclude denial-of-service templates as a safety default.
    cmd = ["nuclei", "-u", url, "-jsonl", "-silent",
           "-exclude-tags", "dos,fuzz", "-rate-limit", "50"]
    rc, out, err = await _run(cmd, timeout)
    findings: list[Finding] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = j.get("info", {})
        sev = (info.get("severity") or "info").lower()
        if sev not in ("info", "low", "medium", "high", "critical"):
            sev = "info"
        findings.append(Finding(
            target=url, tier=1, severity=sev,  # type: ignore[arg-type]
            title=info.get("name", j.get("template-id", "nuclei finding")),
            tool="nuclei",
            evidence=j.get("matched-at", j.get("host", "")),
            remediation=(info.get("remediation") or ""),
            references=info.get("reference") or [],
        ))
    if not findings:
        findings.append(Finding(target=url, tier=1, severity="info",
                                title="nuclei: no template matches",
                                tool="nuclei", evidence=err[:200]))
    return findings


# ---------------------------------------------------------------------------
# testssl.sh
# ---------------------------------------------------------------------------
async def testssl_scan(host: str, timeout: int = _DEFAULT_TIMEOUT) -> list[Finding]:
    binary = "testssl.sh" if _have("testssl.sh") else ("testssl" if _have("testssl") else None)
    if binary is None:
        return [_missing_tool_finding(
            host, "testssl.sh",
            "Install from https://github.com/drwetter/testssl.sh (clone & add to PATH).")]

    cmd = [binary, "--quiet", "--color", "0", "--severity", "LOW", host]
    rc, out, err = await _run(cmd, timeout)
    findings: list[Finding] = []
    # testssl free-text output: surface lines flagged with a severity keyword.
    for line in out.splitlines():
        low = line.lower()
        if any(k in low for k in ("vulnerable", "not ok", "high", "critical", "warn")):
            sev = "high" if ("critical" in low or "vulnerable" in low) else "medium"
            findings.append(Finding(
                target=host, tier=1, severity=sev,  # type: ignore[arg-type]
                title="testssl.sh flagged a TLS issue",
                tool="testssl.sh", evidence=line.strip()[:300],
                remediation="Review server TLS configuration per testssl output.",
            ))
    if not findings:
        findings.append(Finding(target=host, tier=1, severity="info",
                                title="testssl.sh: no LOW+ issues flagged",
                                tool="testssl.sh", evidence=""))
    return findings
