"""
A.V.E.N.G.E.R.S Ops
===================
Deterministic capability handlers behind each persona.

Every handler is `async def op(args: dict) -> dict | None` and returns:
    {
        "summary": str,      # short factual digest fed to the orchestrator
        "data":    Any,      # structured payload for the UI
        "final":   bool,     # True -> skip LLM, speak the summary directly
    }
or None when the message carries no actionable structured command for that
persona (the dispatcher then falls through to a pure persona conversation,
which still runs the full Telugu/emotion/RAG orchestrator pipeline).

Net-new local-first systems implemented here (no fake APIs):
  * OracleOps  -- webhook registry + dispatcher (httpx; n8n-compatible)
  * CrmOps     -- SQLite pipeline (ATHENA) + leads (ZEUS) store
  * AtlasOps   -- ip-api.com geolocation (free endpoint, no key)
  * SpectreOps -- local document compliance scanner
  * JeromeOps  -- Win32 media keys via PowerShell keybd_event
  * ThorOps    -- local node telemetry + Ollama mesh + THOR_NODES probes
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from src.core.logger import log

_DATA_DIR = Path("data")
_DATA_DIR.mkdir(exist_ok=True)
_DB_PATH = _DATA_DIR / "avengers.db"
_LOG_PATH = Path("logs/tron_x.log")


def _result(summary: str, data: Any = None, final: bool = False) -> dict:
    return {"summary": summary, "data": data, "final": final}


# ---------------------------------------------------------------------------
# SQLite store (ATHENA pipeline + ZEUS leads + ORACLE webhooks)
# ---------------------------------------------------------------------------

class _Store:
    """Tiny thread-offloaded SQLite wrapper. One file: data/avengers.db."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS pipeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client TEXT NOT NULL,
        stage TEXT NOT NULL DEFAULT 'lead',
        value REAL DEFAULT 0,
        note TEXT DEFAULT '',
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        contact TEXT DEFAULT '',
        source TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS webhooks (
        name TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        created_at REAL NOT NULL,
        last_fired_at REAL,
        last_status INTEGER
    );
    """

    def __init__(self) -> None:
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(_DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(self._SCHEMA)

    async def run(self, sql: str, params: tuple = (), fetch: bool = False) -> list[dict]:
        def _exec() -> list[dict]:
            with self._connect() as conn:
                cur = conn.execute(sql, params)
                if fetch:
                    return [dict(r) for r in cur.fetchall()]
                conn.commit()
                return [{"rowcount": cur.rowcount, "lastrowid": cur.lastrowid}]
        return await asyncio.to_thread(_exec)


_store: Optional[_Store] = None


def get_store() -> _Store:
    global _store
    if _store is None:
        _store = _Store()
    return _store


# ---------------------------------------------------------------------------
# ORACLE -- Workflow Automation (webhooks / n8n)
# ---------------------------------------------------------------------------

class OracleOps:

    _REGISTER = re.compile(
        r"(?:register|add)\s+webhook\s+(?P<name>[\w\-]+)\s+(?:at\s+|->\s*|to\s+)?(?P<url>https?://\S+)", re.I)
    _TRIGGER = re.compile(r"(?:trigger|fire|run)\s+(?:webhook|workflow)\s+(?P<name>[\w\-]+)", re.I)
    _REMOVE = re.compile(r"(?:remove|delete)\s+webhook\s+(?P<name>[\w\-]+)", re.I)
    _LIST = re.compile(r"(?:list|show)\s+(?:webhooks?|workflows?)", re.I)

    async def handle(self, message: str) -> dict | None:
        store = get_store()
        if m := self._REGISTER.search(message):
            name, url = m.group("name"), m.group("url").rstrip(".,)")
            await store.run(
                "INSERT OR REPLACE INTO webhooks (name, url, created_at) VALUES (?,?,?)",
                (name, url, time.time()))
            return _result(f"Webhook '{name}' registered -> {url}", {"name": name, "url": url}, final=True)

        if m := self._TRIGGER.search(message):
            name = m.group("name")
            rows = await store.run("SELECT * FROM webhooks WHERE name=?", (name,), fetch=True)
            if not rows:
                return _result(f"No webhook named '{name}' is registered.", final=True)
            url = rows[0]["url"]
            status = 0
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(url, json={
                        "source": "tron_x.oracle", "fired_at": time.time(), "trigger": message})
                    status = resp.status_code
            except Exception as e:
                await store.run("UPDATE webhooks SET last_fired_at=?, last_status=? WHERE name=?",
                                (time.time(), -1, name))
                return _result(f"Webhook '{name}' dispatch FAILED: {e}", {"name": name, "error": str(e)}, final=True)
            await store.run("UPDATE webhooks SET last_fired_at=?, last_status=? WHERE name=?",
                            (time.time(), status, name))
            return _result(f"Webhook '{name}' fired -> HTTP {status}",
                           {"name": name, "url": url, "status": status}, final=True)

        if m := self._REMOVE.search(message):
            name = m.group("name")
            res = await store.run("DELETE FROM webhooks WHERE name=?", (name,))
            ok = res[0]["rowcount"] > 0
            return _result(f"Webhook '{name}' {'removed' if ok else 'not found'}.", final=True)

        if self._LIST.search(message):
            rows = await store.run("SELECT * FROM webhooks ORDER BY created_at", fetch=True)
            if not rows:
                return _result("No webhooks registered yet. Say: register webhook <name> <url>", final=True)
            lines = [f"{r['name']} -> {r['url']} (last status: {r['last_status'] or 'never fired'})" for r in rows]
            return _result("Registered webhooks:\n" + "\n".join(lines), rows, final=True)
        return None


# ---------------------------------------------------------------------------
# ATHENA + ZEUS -- CRM (pipeline + leads)
# ---------------------------------------------------------------------------

class CrmOps:

    _ATHENA_ADD = re.compile(
        r"(?:add|create)\s+(?:client|deal)\s+(?P<client>[\w .\-]+?)"
        r"(?:\s+(?:at\s+)?stage\s+(?P<stage>\w+))?(?:\s+worth\s+(?P<value>[\d.]+))?$", re.I)
    _ATHENA_MOVE = re.compile(
        r"(?:move|update)\s+(?:client|deal)\s+(?P<client>[\w .\-]+?)\s+to\s+(?P<stage>\w+)", re.I)
    _ZEUS_ADD = re.compile(
        r"add\s+lead\s+(?P<name>[\w .\-]+?)"
        r"(?:\s+(?:from|via)\s+(?P<source>[\w\-]+))?(?:\s+(?:contact|email|phone)\s+(?P<contact>\S+))?$", re.I)
    _ZEUS_SEARCH = re.compile(r"(?:search|find)\s+leads?\s+(?P<q>.+)", re.I)

    async def athena(self, message: str) -> dict | None:
        store = get_store()
        if m := self._ATHENA_MOVE.search(message):
            client, stage = m.group("client").strip(), m.group("stage").lower()
            res = await store.run(
                "UPDATE pipeline SET stage=?, updated_at=? WHERE client LIKE ?",
                (stage, time.time(), f"%{client}%"))
            if res[0]["rowcount"]:
                return _result(f"Pipeline updated: '{client}' -> stage {stage}.", final=True)
            return _result(f"No pipeline entry matches '{client}'.", final=True)

        if m := self._ATHENA_ADD.search(message.strip()):
            client = m.group("client").strip()
            stage = (m.group("stage") or "lead").lower()
            value = float(m.group("value") or 0)
            await store.run(
                "INSERT INTO pipeline (client, stage, value, updated_at) VALUES (?,?,?,?)",
                (client, stage, value, time.time()))
            return _result(f"Client '{client}' added to pipeline at stage '{stage}'"
                           + (f" worth {value:g}." if value else "."), final=True)

        rows = await store.run("SELECT * FROM pipeline ORDER BY updated_at DESC", fetch=True)
        if not rows:
            return _result("Pipeline is empty. Say: add client <name> stage <stage> worth <value>",
                           {"pipeline": []}, final=False)
        by_stage: dict[str, list] = {}
        total = 0.0
        for r in rows:
            by_stage.setdefault(r["stage"], []).append(r["client"])
            total += r["value"] or 0
        lines = [f"{stage.upper()}: {', '.join(names)}" for stage, names in by_stage.items()]
        return _result(
            f"Pipeline: {len(rows)} entries, total tracked value {total:g}.\n" + "\n".join(lines),
            {"pipeline": rows}, final=False)

    async def zeus(self, message: str) -> dict | None:
        store = get_store()
        if m := self._ZEUS_ADD.search(message.strip()):
            name = m.group("name").strip()
            await store.run(
                "INSERT INTO leads (name, contact, source, created_at) VALUES (?,?,?,?)",
                (name, m.group("contact") or "", m.group("source") or "", time.time()))
            return _result(f"Lead '{name}' stored in the database.", final=True)

        if m := self._ZEUS_SEARCH.search(message):
            q = f"%{m.group('q').strip()}%"
            rows = await store.run(
                "SELECT * FROM leads WHERE name LIKE ? OR contact LIKE ? OR source LIKE ?",
                (q, q, q), fetch=True)
            if not rows:
                return _result("No matching lead records.", final=True)
            lines = [f"#{r['id']} {r['name']} ({r['contact'] or 'no contact'}) via {r['source'] or '?'}" for r in rows]
            return _result(f"{len(rows)} lead(s):\n" + "\n".join(lines), {"leads": rows}, final=False)

        rows = await store.run("SELECT * FROM leads ORDER BY created_at DESC LIMIT 25", fetch=True)
        if not rows:
            return _result("Lead database is empty. Say: add lead <name> from <source>",
                           {"leads": []}, final=False)
        lines = [f"#{r['id']} {r['name']} ({r['contact'] or 'no contact'}) via {r['source'] or '?'}" for r in rows]
        return _result(f"Active lead records ({len(rows)}):\n" + "\n".join(lines),
                       {"leads": rows}, final=False)


# ---------------------------------------------------------------------------
# STARK -- Project Matrix (git telemetry; code tasks go through CodeAgent)
# ---------------------------------------------------------------------------

class StarkOps:

    async def _git(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path.cwd()))
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return proc.returncode or 0, out.decode("utf-8", "replace").strip()

    async def handle(self, message: str) -> dict | None:
        low = message.lower()
        wants_commits = any(k in low for k in ("commit", "git log", "recent changes"))
        wants_status = any(k in low for k in ("repo status", "build status", "project matrix", "git status"))
        if not (wants_commits or wants_status):
            return None  # plain coding request -> dispatcher routes to orchestrator coding intent
        try:
            code, branch = await self._git("rev-parse", "--abbrev-ref", "HEAD")
            if code != 0:
                return _result(f"Not a git repository here: {branch}", final=True)
            _, status = await self._git("status", "--short")
            _, logout = await self._git("log", "--oneline", "-10")
            dirty = len([line for line in status.splitlines() if line.strip()])
            summary = (f"Repo branch '{branch}': {dirty} modified file(s).\n"
                       f"Last commits:\n{logout or '(no commits)'}")
            return _result(summary, {"branch": branch, "dirty_files": dirty,
                                     "commits": logout.splitlines()}, final=False)
        except FileNotFoundError:
            return _result("git binary not found on PATH.", final=True)
        except Exception as e:
            return _result(f"Git telemetry failed: {e}", final=True)


# ---------------------------------------------------------------------------
# STEVE -- Build Ops (deploy/ folder, confirmation-gated)
# ---------------------------------------------------------------------------

class SteveOps:

    _DEPLOY_DIR = Path("deploy")
    _RUN = re.compile(r"(?:run|execute|trigger)\s+deploy(?:ment)?\s+(?P<script>[\w.\-]+)(?P<confirm>\s+confirm)?", re.I)

    async def handle(self, message: str) -> dict | None:
        if m := self._RUN.search(message):
            script = m.group("script")
            path = self._DEPLOY_DIR / script
            if not path.exists():
                return _result(f"No '{script}' in deploy/. Assets: "
                               + ", ".join(p.name for p in self._DEPLOY_DIR.iterdir()), final=True)
            if not m.group("confirm"):
                return _result(
                    f"Deployment armed for '{script}'. Safety gate engaged -- say "
                    f"\"run deploy {script} confirm\" to execute.", final=True)
            suffix = path.suffix.lower()
            try:
                if suffix in (".sh", ".bash"):
                    from src.system.executor import execute_bash
                    res = await execute_bash(f"bash {path}", timeout=120)
                elif suffix == ".ps1":
                    from src.system.powershell import run_powershell
                    res = await run_powershell(str(path), timeout=120)
                elif suffix == ".py":
                    from src.system.executor import execute_python_safe
                    res = await execute_python_safe(path.read_text(encoding="utf-8"), timeout=120)
                else:
                    return _result(f"'{script}' is a {suffix or 'plain'} asset, not an executable "
                                   "deploy script (.sh/.ps1/.py).", final=True)
                return _result(f"Deploy script '{script}' executed.", res, final=False)
            except Exception as e:
                return _result(f"Deploy execution failed: {e}", final=True)

        # default: list assets
        if not self._DEPLOY_DIR.exists():
            return _result("deploy/ folder not found.", final=True)
        assets = sorted(p.name for p in self._DEPLOY_DIR.iterdir() if p.is_file())
        return _result("Deployment assets: " + (", ".join(assets) or "(empty)")
                       + ". Say \"run deploy <script> confirm\" to execute one.",
                       {"assets": assets}, final=False)


# ---------------------------------------------------------------------------
# HERALD -- Meeting Transceiver
# ---------------------------------------------------------------------------

class HeraldOps:

    async def transcribe_b64(self, audio_b64: str, filename: str = "meeting.webm") -> dict:
        from src.voice.stt import get_stt
        audio = base64.b64decode(audio_b64)
        res = await get_stt().transcribe(audio, filename=filename)
        return _result(f"Transcript ({res.get('provider')}): {res.get('text', '')}",
                       res, final=False)

    async def handle(self, message: str) -> dict | None:
        # Pure-text path: action-item extraction happens in the LLM leg with
        # the HERALD overlay; no deterministic op needed here.
        return None


# ---------------------------------------------------------------------------
# VISION -- System Overwatch
# ---------------------------------------------------------------------------

class VisionOps:

    async def handle(self, message: str) -> dict | None:
        from src.system.control import get_system_info, list_processes
        info = await get_system_info()
        data: dict[str, Any] = {"system": info}
        summary_parts = []
        if isinstance(info, dict):
            for key in ("os", "cpu_percent", "memory_percent", "disk_percent",
                        "memory", "disk", "cpu"):
                if key in info:
                    summary_parts.append(f"{key}={info[key]}")
        if "process" in message.lower():
            procs = await list_processes(sort_by="cpu")
            data["processes"] = procs
        summary = "System overwatch: " + (", ".join(str(p) for p in summary_parts[:6])
                                          or json.dumps(info, default=str)[:400])
        return _result(summary, data, final=False)


# ---------------------------------------------------------------------------
# BANNER -- Diagnostics Lab
# ---------------------------------------------------------------------------

class BannerOps:

    async def handle(self, message: str) -> dict | None:
        def _hardware() -> dict:
            import psutil
            diag: dict[str, Any] = {
                "cpu_percent": psutil.cpu_percent(interval=0.3),
                "cpu_count": psutil.cpu_count(),
                "ram_percent": psutil.virtual_memory().percent,
                "ram_total_gb": round(psutil.virtual_memory().total / 1e9, 1),
                "swap_percent": psutil.swap_memory().percent,
            }
            try:
                batt = psutil.sensors_battery()
                if batt:
                    diag["battery_percent"] = batt.percent
                    diag["plugged_in"] = batt.power_plugged
            except Exception:
                pass
            try:
                temps = psutil.sensors_temperatures()
                if temps:
                    diag["temperatures"] = {
                        k: [round(t.current, 1) for t in v] for k, v in temps.items()}
            except Exception:
                pass
            for part in __import__("psutil").disk_partitions(all=False):
                try:
                    usage = psutil.disk_usage(part.mountpoint)
                    diag.setdefault("disks", {})[part.mountpoint] = f"{usage.percent}% used"
                except Exception:
                    continue
            return diag

        hardware = await asyncio.to_thread(_hardware)

        log_errors: list[str] = []
        if _LOG_PATH.exists():
            def _tail_errors() -> list[str]:
                lines = _LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
                return [ln for ln in lines[-800:] if "ERROR" in ln or "WARNING" in ln][-12:]
            log_errors = await asyncio.to_thread(_tail_errors)

        summary = (f"Diagnostics: CPU {hardware.get('cpu_percent')}%, "
                   f"RAM {hardware.get('ram_percent')}%"
                   + (f", battery {hardware.get('battery_percent')}%" if "battery_percent" in hardware else "")
                   + f". {len(log_errors)} recent warning/error log line(s).")
        return _result(summary, {"hardware": hardware, "recent_log_issues": log_errors}, final=False)


# ---------------------------------------------------------------------------
# ULTRON -- Perimeter Security
# ---------------------------------------------------------------------------

class UltronOps:

    _ACTIONABLE = re.compile(r"\b(recon|scan|probe|enumerate)\b", re.I)

    async def handle(self, message: str) -> dict | None:
        if not self._ACTIONABLE.search(message):
            return None  # security chat -> persona conversation
        from src.agents.security_agent import SecurityAgent
        res = await SecurityAgent().run(message)
        return _result(res.get("reply", "Security operation complete."), res,
                       final=not res.get("ok", False))


# ---------------------------------------------------------------------------
# THOR -- Compute Infrastructure
# ---------------------------------------------------------------------------

class ThorOps:

    async def handle(self, message: str) -> dict | None:
        def _local() -> dict:
            import platform
            import psutil
            return {
                "node": platform.node(),
                "os": f"{platform.system()} {platform.release()}",
                "cpu_percent": psutil.cpu_percent(interval=0.2),
                "cores": psutil.cpu_count(),
                "ram_percent": psutil.virtual_memory().percent,
                "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
            }
        local = await asyncio.to_thread(_local)
        nodes: list[dict] = [{"name": "local", "status": "online", **local}]

        # Ollama mesh
        ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{ollama_url}/api/tags")
                models = [m.get("name") for m in resp.json().get("models", [])]
                nodes.append({"name": "ollama-mesh", "status": "online", "models": models})
        except Exception:
            nodes.append({"name": "ollama-mesh", "status": "offline"})

        # Optional remote nodes: THOR_NODES='[{"name":"gpu-box","url":"http://10.0.0.5:8000/health"}]'
        raw = os.environ.get("THOR_NODES", "")
        if raw:
            try:
                for spec in json.loads(raw):
                    try:
                        async with httpx.AsyncClient(timeout=4.0) as client:
                            r = await client.get(spec["url"])
                            nodes.append({"name": spec.get("name", spec["url"]),
                                          "status": "online" if r.status_code < 500 else "degraded",
                                          "http": r.status_code})
                    except Exception as e:
                        nodes.append({"name": spec.get("name", "?"), "status": "offline", "error": str(e)})
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                nodes.append({"name": "THOR_NODES", "status": "config-error", "error": str(e)})

        online = sum(1 for n in nodes if n.get("status") == "online")
        summary = (f"Compute realms: {online}/{len(nodes)} nodes online. "
                   f"Local: CPU {local['cpu_percent']}%, RAM {local['ram_percent']}%, "
                   f"uptime {local['uptime_hours']}h.")
        return _result(summary, {"nodes": nodes}, final=False)


# ---------------------------------------------------------------------------
# ATLAS -- Geolocation Intelligence
# ---------------------------------------------------------------------------

class AtlasOps:

    _TARGET = re.compile(r"(?:geolocate|locate|where\s+is|trace)\s+(?:ip\s+)?(?P<t>[\w.\-]+)", re.I)

    async def handle(self, message: str) -> dict | None:
        m = self._TARGET.search(message)
        target = m.group("t") if m else ""
        if target.lower() in ("me", "i", "self", "this", "my"):
            target = ""
        url = f"http://ip-api.com/json/{target}" if target else "http://ip-api.com/json/"
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, params={
                    "fields": "status,message,query,country,regionName,city,lat,lon,isp,org,as,timezone"})
                geo = resp.json()
        except Exception as e:
            return _result(f"Geolocation lookup failed: {e}", final=True)
        if geo.get("status") != "success":
            return _result(f"Geolocation failed for '{target or 'self'}': {geo.get('message', 'unknown')}",
                           geo, final=True)
        summary = (f"Geo-int on {geo['query']}: {geo.get('city')}, {geo.get('regionName')}, "
                   f"{geo.get('country')} ({geo.get('lat')}, {geo.get('lon')}). "
                   f"ISP: {geo.get('isp')}. TZ: {geo.get('timezone')}.")
        return _result(summary, geo, final=False)


# ---------------------------------------------------------------------------
# HERCULES -- Vision Analysis
# ---------------------------------------------------------------------------

class HerculesOps:

    _PATH = re.compile(r"(?P<p>(?:[A-Za-z]:)?[\w./\\\- ]+\.(?:png|jpe?g|webp|bmp|gif))", re.I)

    async def handle(self, message: str) -> dict | None:
        low = message.lower()
        m = self._PATH.search(message)
        if m and Path(m.group("p").strip()).exists():
            from src.agents.vision_agent import VisionAgent
            path = m.group("p").strip()
            agent = VisionAgent()
            if "ocr" in low or "text" in low:
                out = await agent.extract_text(path)
            elif "chart" in low or "graph" in low:
                out = await agent.analyse_chart(path)
            else:
                out = await agent.describe(path)
            return _result(str(out), {"image_path": path}, final=False)
        if "screen" in low:
            from src.vision.screen import describe_screen
            out = await describe_screen()
            text = out.get("description", str(out)) if isinstance(out, dict) else str(out)
            return _result(f"Screen analysis: {text}", out if isinstance(out, dict) else None, final=False)
        return None


# ---------------------------------------------------------------------------
# DR. STRANGE -- Database Diagnostics
# ---------------------------------------------------------------------------

class StrangeOps:

    async def handle(self, message: str) -> dict | None:
        data: dict[str, Any] = {}
        try:
            from src.memory.chroma_db import get_chroma
            data["chroma"] = get_chroma().stats()
        except Exception as e:
            data["chroma"] = {"error": str(e)}
        try:
            from src.memory.supabase_client import get_supabase
            data["supabase"] = get_supabase().status()
        except Exception as e:
            data["supabase"] = {"error": str(e)}
        chroma_desc = json.dumps(data["chroma"], default=str)[:300]
        supa = data["supabase"]
        supa_desc = ("connected" if isinstance(supa, dict) and supa.get("enabled")
                     else json.dumps(supa, default=str)[:200])
        return _result(f"Data-layer scan -- Chroma: {chroma_desc}. Supabase: {supa_desc}.",
                       data, final=False)


# ---------------------------------------------------------------------------
# SPECTRE -- Legal / Contract Parser
# ---------------------------------------------------------------------------

class SpectreOps:

    _DOCS_DIR = Path(os.environ.get("SPECTRE_DOCS_DIR", "data/docs"))
    _PATH = re.compile(r"(?P<p>(?:[A-Za-z]:)?[\w./\\\- ]+\.(?:txt|md|pdf|docx))", re.I)
    _RISK_TERMS = [
        "indemnif", "liability", "termination", "penalty", "arbitration",
        "non-compete", "confidential", "warranty", "jurisdiction", "auto-renew",
        "exclusivity", "assignment", "force majeure", "limitation of liability",
    ]

    def _read_doc(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in (".txt", ".md"):
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".docx":
            import zipfile
            import xml.etree.ElementTree as ET
            with zipfile.ZipFile(path) as zf:
                xml_bytes = zf.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            return "\n".join(
                "".join(t.text or "" for t in p.iter("{%s}t" % ns["w"]))
                for p in root.iter("{%s}p" % ns["w"]))
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader  # type: ignore[no-redef]
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        return ""

    async def handle(self, message: str) -> dict | None:
        m = self._PATH.search(message)
        targets: list[Path] = []
        if m and Path(m.group("p").strip()).exists():
            targets = [Path(m.group("p").strip())]
        elif self._DOCS_DIR.exists():
            targets = [p for p in self._DOCS_DIR.rglob("*")
                       if p.suffix.lower() in (".txt", ".md", ".pdf", ".docx")][:10]
        if not targets:
            return _result(
                f"No documents found. Drop contracts into {self._DOCS_DIR} or name a file path.",
                final=True)

        def _scan() -> list[dict]:
            findings = []
            for path in targets:
                try:
                    text = self._read_doc(path)
                except Exception as e:
                    findings.append({"doc": path.name, "error": str(e)})
                    continue
                low = text.lower()
                hits = sorted({term for term in self._RISK_TERMS if term in low})
                findings.append({"doc": path.name, "chars": len(text), "risk_terms": hits,
                                 "excerpt": text[:1500]})
            return findings

        findings = await asyncio.to_thread(_scan)
        n_hits = sum(len(f.get("risk_terms", [])) for f in findings)
        summary_lines = [
            f"{f['doc']}: {', '.join(f['risk_terms']) if f.get('risk_terms') else f.get('error', 'no risk terms')}"
            for f in findings]
        return _result(
            f"Compliance sweep over {len(findings)} document(s), {n_hits} risk-term hit(s):\n"
            + "\n".join(summary_lines),
            {"findings": [{k: v for k, v in f.items() if k != 'excerpt'} for f in findings],
             "excerpts": {f['doc']: f.get('excerpt', '') for f in findings}},
            final=False)


# ---------------------------------------------------------------------------
# JALEN -- Market Analytics
# ---------------------------------------------------------------------------

class JalenOps:

    _CRYPTO_WORDS = {"bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum",
                     "eth": "ethereum", "solana": "solana", "sol": "solana",
                     "dogecoin": "dogecoin", "doge": "dogecoin", "xrp": "ripple",
                     "cardano": "cardano", "ada": "cardano", "bnb": "binancecoin"}
    _TICKER = re.compile(r"\b(?:\$|stock\s+)([A-Z]{1,5})\b|\bprice\s+of\s+([A-Z]{1,5})\b")

    async def handle(self, message: str) -> dict | None:
        low = message.lower()
        data: dict[str, Any] = {}
        lines: list[str] = []

        coins = sorted({cid for word, cid in self._CRYPTO_WORDS.items()
                        if re.search(rf"\b{re.escape(word)}\b", low)})
        if coins or "crypto" in low:
            from src.feeds.crypto import get_crypto_feed
            feed = get_crypto_feed()
            if coins:
                for coin in coins[:5]:
                    try:
                        p = await feed.price(coin)
                        data.setdefault("crypto", {})[coin] = p
                        usd = p.get("usd") or (p.get(coin, {}) or {}).get("usd") if isinstance(p, dict) else None
                        lines.append(f"{coin}: {json.dumps(p, default=str)[:160]}" if usd is None
                                     else f"{coin}: ${usd:,}")
                    except Exception as e:
                        lines.append(f"{coin}: feed error ({e})")
            else:
                try:
                    top = await feed.top(limit=5)
                    data["crypto_top"] = top
                    lines.append("Top crypto: " + ", ".join(
                        f"{c.get('symbol', '?').upper()} ${c.get('current_price', '?')}" for c in top))
                except Exception as e:
                    lines.append(f"Crypto top-list error: {e}")

        tickers = sorted({t for pair in self._TICKER.findall(message) for t in pair if t})
        if tickers:
            from src.feeds.stocks import get_stock_feed
            feed = get_stock_feed()
            for sym in tickers[:5]:
                try:
                    q = await feed.quote(sym)
                    data.setdefault("stocks", {})[sym] = q
                    price = q.get("price") or q.get("regularMarketPrice") or q.get("last")
                    lines.append(f"{sym}: ${price}" if price is not None
                                 else f"{sym}: {json.dumps(q, default=str)[:160]}")
                except Exception as e:
                    lines.append(f"{sym}: feed error ({e})")

        if not lines:
            return None
        return _result("Market feed -- " + " | ".join(lines), data, final=False)


# ---------------------------------------------------------------------------
# ANTS -- Swarm Micro-Scheduler
# ---------------------------------------------------------------------------

class AntsOps:

    async def handle(self, message: str) -> dict | None:
        from src.agents.parallel_supervisor import ParallelSupervisorAgent
        goal = re.sub(r"^(?:swarm|parallel(?:ize)?|break\s+this\s+down)[:,]?\s*", "",
                      message, flags=re.I).strip() or message
        res = await ParallelSupervisorAgent().run(goal)
        reply = res.get("reply", "")
        plan = res.get("plan", [])
        summary = (f"Swarm complete: {len(plan)} sub-task(s), "
                   f"max concurrency {res.get('max_concurrency', 0)}, "
                   f"{res.get('ticks', 0)} tick(s).\n{reply}")
        return _result(summary, res, final=False)


# ---------------------------------------------------------------------------
# JEROME -- Media Controller
# ---------------------------------------------------------------------------

class JeromeOps:

    # Win32 virtual-key codes (standard, documented): keybd_event media keys.
    _VK = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}

    _VOL = re.compile(r"volume\s+(?:to\s+)?(\d{1,3})", re.I)
    _OPEN = re.compile(r"open\s+(?P<app>[\w .\-]+)", re.I)

    async def _media_key(self, key: str) -> dict:
        vk = self._VK[key]
        ps = (
            "Add-Type -TypeDefinition '"
            "using System;using System.Runtime.InteropServices;"
            "public static class MediaKey{"
            "[DllImport(\"user32.dll\")]"
            "public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);"
            "}'; "
            f"[MediaKey]::keybd_event({vk},0,0,[UIntPtr]::Zero); "
            f"[MediaKey]::keybd_event({vk},0,2,[UIntPtr]::Zero)"
        )
        from src.system.powershell import run_powershell
        return await run_powershell(ps, timeout=10)

    async def handle(self, message: str) -> dict | None:
        low = message.lower()
        try:
            if any(k in low for k in ("pause", "play", "resume")):
                res = await self._media_key("play_pause")
                return _result("Media play/pause toggled.", res, final=True)
            if "next" in low and ("track" in low or "song" in low or "media" in low):
                res = await self._media_key("next")
                return _result("Skipped to next track.", res, final=True)
            if ("previous" in low or "prev " in low) and ("track" in low or "song" in low):
                res = await self._media_key("prev")
                return _result("Back to previous track.", res, final=True)
            if "mute" in low:
                from src.system.control import mute
                res = await mute()
                return _result("Audio mute toggled.", res, final=True)
            if m := self._VOL.search(message):
                from src.system.control import set_volume
                level = max(0, min(100, int(m.group(1))))
                res = await set_volume(level)
                return _result(f"Volume set to {level}%.", res, final=True)
            if m := self._OPEN.search(message):
                from src.system.control import open_app
                app = m.group("app").strip()
                res = await open_app(app)
                return _result(f"Launching {app}.", res, final=True)
        except Exception as e:
            return _result(f"Media control failed: {e}", final=True)
        return None


# ---------------------------------------------------------------------------
# HULK -- Offline Fail-Safe
# ---------------------------------------------------------------------------

class HulkOps:

    async def handle(self, message: str) -> dict | None:
        low = message.lower()
        from src.system.backup import list_backups
        if "create" in low and "backup" in low:
            try:
                from src.system.backup import create_backup
                path = await create_backup()
                return _result(f"Backup created: {path.name}. HULK keep data safe.",
                               {"path": str(path)}, final=True)
            except Exception as e:
                return _result(f"Backup failed: {e}", final=True)
        backups = [str(p.name) for p in list_backups()]
        # Local fallback readiness: is the Ollama mesh reachable?
        ollama_ok = False
        try:
            ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            async with httpx.AsyncClient(timeout=2.5) as client:
                ollama_ok = (await client.get(f"{ollama_url}/api/tags")).status_code == 200
        except Exception:
            pass
        summary = (f"Fail-safe status: {len(backups)} backup(s) on disk. "
                   f"Local fallback models (Ollama): {'ONLINE -- cloud outage survivable' if ollama_ok else 'offline'}.")
        return _result(summary, {"backups": backups, "ollama_fallback": ollama_ok}, final=False)


# ---------------------------------------------------------------------------
# PEPPER -- Admin / Scheduling
# ---------------------------------------------------------------------------

class PepperOps:

    _REMIND = re.compile(r"remind\s+me\s+(?:to\s+)?(?P<what>.+?)\s+(?P<when>(?:in|at|on|tomorrow|tonight|next)\b.*)$", re.I)

    async def handle(self, message: str) -> dict | None:
        low = message.lower()
        try:
            if m := self._REMIND.search(message):
                from src.agents.reminder_agent import get_reminder_agent
                res = await get_reminder_agent().set_reminder_nl(
                    m.group("what").strip(), m.group("when").strip())
                return _result(f"Reminder set: {m.group('what').strip()} ({m.group('when').strip()}).",
                               res, final=True)
            if "reminder" in low and any(k in low for k in ("list", "show", "what")):
                from src.agents.reminder_agent import get_reminder_agent
                res = await get_reminder_agent().list_reminders()
                items = res.get("reminders", res) if isinstance(res, dict) else res
                return _result(f"Reminders on file: {json.dumps(items, default=str)[:600]}", res, final=False)
            if "inbox" in low or ("email" in low and any(k in low for k in ("summar", "check", "unread", "new"))):
                from src.agents.email_agent import EmailAgent
                res = await EmailAgent().summarize_inbox()
                reply = res.get("summary", res.get("reply", "")) if isinstance(res, dict) else str(res)
                return _result(f"Inbox briefing: {reply}", res if isinstance(res, dict) else None, final=False)
        except Exception as e:
            return _result(f"Admin operation failed: {e}", final=True)
        return None


# ---------------------------------------------------------------------------
# Singleton accessors
# ---------------------------------------------------------------------------

oracle_ops = OracleOps()
crm_ops = CrmOps()
stark_ops = StarkOps()
steve_ops = SteveOps()
herald_ops = HeraldOps()
vision_ops = VisionOps()
banner_ops = BannerOps()
ultron_ops = UltronOps()
thor_ops = ThorOps()
atlas_ops = AtlasOps()
hercules_ops = HerculesOps()
strange_ops = StrangeOps()
spectre_ops = SpectreOps()
jalen_ops = JalenOps()
ants_ops = AntsOps()
jerome_ops = JeromeOps()
hulk_ops = HulkOps()
pepper_ops = PepperOps()
