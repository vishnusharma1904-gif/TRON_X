"""
TRON-X Tier 0 — Passive / Non-Intrusive Recon
----------------------------------------------
Pure-Python checks that need no external binaries. Safe to run; they perform
ordinary lookups and a single TLS handshake / HTTP HEAD-GET.

    findings = await recon_host("example.com")
    findings = await recon_url("https://example.com")
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import datetime as _dt
from typing import Optional

import httpx

from src.core.logger import log
from src.security.report import Finding

# Security headers we flag when missing, with remediation hints.
_SECURITY_HEADERS = {
    "strict-transport-security": "Add HSTS: 'Strict-Transport-Security: max-age=31536000; includeSubDomains'.",
    "content-security-policy": "Define a restrictive Content-Security-Policy to mitigate XSS/data injection.",
    "x-content-type-options": "Set 'X-Content-Type-Options: nosniff'.",
    "x-frame-options": "Set 'X-Frame-Options: DENY' (or a CSP frame-ancestors directive).",
    "referrer-policy": "Set a 'Referrer-Policy' such as 'strict-origin-when-cross-origin'.",
    "permissions-policy": "Set a 'Permissions-Policy' to restrict powerful browser features.",
}


async def recon_host(host: str) -> list[Finding]:
    """DNS + TLS-certificate recon for a bare host."""
    findings: list[Finding] = []
    findings += await _dns_info(host)
    findings += await _tls_cert_info(host)
    return findings


async def recon_url(url: str) -> list[Finding]:
    """HTTP security-header analysis + host recon for a URL."""
    if "://" not in url:
        url = "https://" + url
    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    findings: list[Finding] = []
    findings += await recon_host(host)
    findings += await _security_headers(url)
    return findings


async def _dns_info(host: str) -> list[Finding]:
    loop = asyncio.get_event_loop()
    out: list[Finding] = []
    try:
        infos = await loop.getaddrinfo(host, None)
        addrs = sorted({i[4][0] for i in infos})
        out.append(Finding(
            target=host, tier=0, severity="info",
            title="Resolved addresses",
            tool="dns", evidence=", ".join(addrs),
        ))
    except Exception as ex:  # noqa: BLE001
        out.append(Finding(
            target=host, tier=0, severity="info",
            title="DNS resolution failed", tool="dns", evidence=str(ex),
        ))
    return out


async def _tls_cert_info(host: str, port: int = 443) -> list[Finding]:
    loop = asyncio.get_event_loop()

    def _fetch():
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                return ss.getpeercert(), ss.version()

    out: list[Finding] = []
    try:
        cert, proto = await loop.run_in_executor(None, _fetch)
    except Exception as ex:  # noqa: BLE001
        out.append(Finding(
            target=host, tier=0, severity="info",
            title="TLS handshake failed (no HTTPS on :443?)",
            tool="tls", evidence=str(ex),
        ))
        return out

    not_after = cert.get("notAfter")
    sev: str = "info"
    evid = f"protocol={proto}, issuer={_cert_field(cert, 'issuer')}, expires={not_after}"
    try:
        exp = _dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
        days = (exp - _dt.datetime.utcnow()).days
        evid += f", days_left={days}"
        if days < 0:
            sev = "high"
        elif days < 14:
            sev = "medium"
    except Exception:  # noqa: BLE001
        pass

    out.append(Finding(
        target=host, tier=0, severity=sev,  # type: ignore[arg-type]
        title="TLS certificate", tool="tls", evidence=evid,
        remediation="Renew before expiry; use a trusted CA." if sev != "info" else "",
    ))
    if proto in ("TLSv1", "TLSv1.1", "SSLv3"):
        out.append(Finding(
            target=host, tier=0, severity="medium",
            title=f"Outdated TLS protocol negotiated ({proto})",
            tool="tls", evidence=f"negotiated {proto}",
            remediation="Disable TLS < 1.2 on the server.",
            references=["CWE-326"],
        ))
    return out


def _cert_field(cert: dict, key: str) -> str:
    try:
        parts = cert.get(key, ())
        return ", ".join(f"{k}={v}" for rdn in parts for (k, v) in rdn)
    except Exception:  # noqa: BLE001
        return ""


async def _security_headers(url: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10,
                                     verify=True) as client:
            r = await client.get(url)
    except Exception as ex:  # noqa: BLE001
        out.append(Finding(
            target=url, tier=0, severity="info",
            title="HTTP request failed", tool="header-audit", evidence=str(ex),
        ))
        return out

    headers = {k.lower(): v for k, v in r.headers.items()}
    host = url.split("://", 1)[1].split("/", 1)[0]

    for name, fix in _SECURITY_HEADERS.items():
        if name not in headers:
            out.append(Finding(
                target=host, tier=0, severity="low",
                title=f"Missing security header: {name}",
                tool="header-audit", evidence=f"HTTP {r.status_code} from {url}",
                remediation=fix, references=["CWE-693"],
            ))

    server = headers.get("server")
    if server:
        out.append(Finding(
            target=host, tier=0, severity="info",
            title="Server banner disclosed", tool="header-audit",
            evidence=f"Server: {server}",
            remediation="Consider suppressing version details in the Server header.",
        ))
    powered = headers.get("x-powered-by")
    if powered:
        out.append(Finding(
            target=host, tier=0, severity="low",
            title="Technology disclosure via X-Powered-By",
            tool="header-audit", evidence=f"X-Powered-By: {powered}",
            remediation="Remove the X-Powered-By header.",
        ))
    return out
