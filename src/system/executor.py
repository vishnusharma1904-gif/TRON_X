"""
TRON-X Code Executor
──────────────────────
Sandboxed Python execution in a subprocess with timeout.
stdout/stderr captured. NOTE: this is a soft sandbox — it restricts imports
via an AST scan but does NOT block network access at the OS level. Do not run
fully untrusted code without an external sandbox (container / seccomp).
"""
from __future__ import annotations

import asyncio
import sys
import textwrap
from typing import Optional

from src.core.logger import log


async def _kill_proc(proc) -> None:
    """Terminate a subprocess that overran its timeout so it can't keep running."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
    except ProcessLookupError:
        pass
    except Exception as e:  # pragma: no cover - best-effort cleanup
        log.warning("[executor] Failed to kill timed-out process: %s", e)

# Default sandbox restrictions
_TIMEOUT_SECONDS = 15
_MAX_OUTPUT_CHARS = 8000

# Network-related blocks — these are the only ones relaxed by allow_network=True
_NETWORK_IMPORTS = frozenset([
    "socket", "urllib.request", "http.client", "ftplib", "smtplib",
])
# Always-blocked regardless of allow_network
_DANGEROUS_IMPORTS = frozenset([
    "subprocess", "os.system", "ctypes", "shutil",
])


def _precheck(code: str, allow_network: bool = False) -> Optional[str]:
    """Quick static check for obviously dangerous patterns.

    allow_network only relaxes the network-related entries; subprocess/ctypes/
    shutil are always blocked.
    """
    lowered = code.lower()
    blocked = set(_DANGEROUS_IMPORTS)
    if not allow_network:
        blocked |= _NETWORK_IMPORTS
    for token in blocked:
        if token in lowered:
            return f"Blocked module/call detected: '{token}'"
    return None


async def execute_python(
    code: str,
    timeout: int = _TIMEOUT_SECONDS,
    allow_network: bool = False,
) -> dict:
    """
    Execute Python code in an isolated subprocess.
    Returns stdout, stderr, return_code, and any error.
    """
    # Static safety check (allow_network only relaxes network imports)
    warning = _precheck(code, allow_network=allow_network)
    if warning:
        return {
            "success": False,
            "blocked": True,
            "reason": warning,
            "code": code,
        }

    # Wrap code to capture output
    wrapped = textwrap.dedent(f"""
import sys, io, traceback

_out = io.StringIO()
_err = io.StringIO()
sys.stdout = _out
sys.stderr = _err

try:
{textwrap.indent(code, '    ')}
except Exception:
    traceback.print_exc(file=_err)

sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__
print("__STDOUT__", _out.getvalue(), sep="")
print("__STDERR__", _err.getvalue(), sep="", file=sys.stderr)
""")

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )

        out_text = stdout.decode(errors="replace").replace("__STDOUT__", "", 1).strip()
        err_text = stderr.decode(errors="replace").replace("__STDERR__", "", 1).strip()

        # Trim large outputs
        if len(out_text) > _MAX_OUTPUT_CHARS:
            out_text = out_text[:_MAX_OUTPUT_CHARS] + "\n... [TRUNCATED]"

        log.info(f"[executor] Code exec rc={proc.returncode}")
        return {
            "success":     proc.returncode == 0,
            "return_code": proc.returncode,
            "stdout":      out_text,
            "stderr":      err_text,
        }

    except asyncio.TimeoutError:
        await _kill_proc(locals().get("proc"))
        return {"success": False, "error": f"Execution timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def execute_and_explain(code: str, persona: str = "jarvis") -> dict:
    """
    Execute code and ask LLM to explain the result.
    """
    result = await execute_python(code)

    if result.get("blocked"):
        explanation = f"Execution was blocked: {result['reason']}"
    else:
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        output_summary = (
            f"Code:\n```python\n{code}\n```\n"
            f"Return code: {result.get('return_code', 'N/A')}\n"
            f"Stdout:\n{result.get('stdout', '(none)')}\n"
            f"Stderr:\n{result.get('stderr', '(none)')}"
        )
        resp = await orch.chat(
            user_message=f"Briefly explain this code execution result:\n{output_summary}",
            session_id="__executor__",
            intent="coding",
            persona=persona,
            max_tokens=400,
        )
        explanation = resp.get("reply", "")

    return {**result, "explanation": explanation}


# =============================================================================
# Phase 7 additions -- AST scanner, JS/Bash runners, auto-install, wall time
# =============================================================================
import ast
import re
import time as _time

# Modules blocked by AST scan
_BLOCKED_MODULES = frozenset({
    "os", "subprocess", "socket", "ctypes", "shutil",
    "urllib", "http", "ftplib", "smtplib", "importlib",
    "pty", "multiprocessing", "builtins", "pickle", "shelve",
})

# Safe bash command prefixes (whitelist)
_BASH_WHITELIST = {
    "echo", "ls", "cat", "pwd", "date", "python3", "python", "node",
    "grep", "find", "wc", "sort", "head", "tail", "uniq", "cut",
    "awk", "sed", "tr", "diff", "which", "whoami", "hostname",
    "env", "printenv", "uname", "uptime",
}

# Bash patterns to block outright
_BASH_BLOCKED = re.compile(
    r"(rm\s|sudo|chmod|chown|dd\s|mkfs|fdisk|curl\s|wget\s|"
    r"nc\s|ncat\s|netcat\s|>/dev/|&\s*$|\beval\b|\$\(|`)",
    re.IGNORECASE,
)


def _ast_scan(code: str) -> str | None:
    """AST-based safety scan for Python code. Returns error string or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return f"Blocked import: '{alias.name}'"

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _BLOCKED_MODULES:
                    return f"Blocked import: 'from {node.module}'"

        elif isinstance(node, ast.Call):
            # __import__('os')
            if isinstance(node.func, ast.Name) and node.func.id == "__import__":
                return "Blocked: __import__() call"
            # exec() / eval()
            if isinstance(node.func, ast.Name) and node.func.id in ("exec", "eval"):
                return f"Blocked: {node.func.id}() call"

    return None


async def _auto_install(package: str) -> dict:
    """Try to pip-install a missing package. Returns install result dict."""
    log.info(f"[executor] Auto-installing: {package}")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", package,
            "--break-system-packages", "--quiet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        success = proc.returncode == 0
        log.info(f"[executor] pip install {package} -> rc={proc.returncode}")
        return {"success": success, "package": package,
                "output": (stdout + stderr).decode(errors="replace").strip()[:500]}
    except Exception as e:
        return {"success": False, "package": package, "error": str(e)}


async def execute_python_safe(
    code: str,
    timeout: int = _TIMEOUT_SECONDS,
    auto_install: bool = True,
) -> dict:
    """
    AST-scanned Python executor with wall-time measurement and auto-install.
    Replaces the naive string-match _precheck with proper AST analysis.
    """
    # AST scan first
    block_reason = _ast_scan(code)
    if block_reason:
        return {"success": False, "blocked": True, "reason": block_reason, "code": code}

    async def _run(code: str) -> dict:
        wrapped = textwrap.dedent(f"""
import sys, io, traceback
_out = io.StringIO()
_err = io.StringIO()
sys.stdout = _out
sys.stderr = _err
try:
{textwrap.indent(code, '    ')}
except Exception:
    traceback.print_exc(file=_err)
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__
print("__STDOUT__", _out.getvalue(), sep="")
print("__STDERR__", _err.getvalue(), sep="", file=sys.stderr)
""")
        t0 = _time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_proc(proc)
            raise
        wall_ms = round((_time.perf_counter() - t0) * 1000, 1)
        out = stdout.decode(errors="replace").replace("__STDOUT__", "", 1).strip()
        err = stderr.decode(errors="replace").replace("__STDERR__", "", 1).strip()
        if len(out) > _MAX_OUTPUT_CHARS:
            out = out[:_MAX_OUTPUT_CHARS] + "\n... [TRUNCATED]"
        return {"success": proc.returncode == 0, "return_code": proc.returncode,
                "stdout": out, "stderr": err, "wall_ms": wall_ms}

    try:
        result = await _run(code)

        # Auto-install on ModuleNotFoundError then retry once
        if auto_install and not result["success"]:
            match = re.search(r"ModuleNotFoundError: No module named '([^']+)'", result["stderr"])
            if match:
                pkg = match.group(1).split(".")[0]
                install = await _auto_install(pkg)
                if install["success"]:
                    result = await _run(code)
                    result["auto_installed"] = pkg

        log.info(f"[executor] python_safe rc={result['return_code']} wall={result['wall_ms']}ms")
        return result

    except asyncio.TimeoutError:
        return {"success": False, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def execute_js(code: str, timeout: int = 15) -> dict:
    """Run JavaScript via Node.js. Blocks require('child_process') and require('fs')."""
    _JS_BLOCKED = re.compile(
        r"require\s*\(\s*['\"]"
        r"(child_process|fs|net|http|https|crypto|os|path|cluster|worker_threads)"
        r"['\"]",
        re.IGNORECASE,
    )
    if _JS_BLOCKED.search(code):
        return {"success": False, "blocked": True,
                "reason": "Blocked: dangerous require() detected"}

    # Check node is available
    try:
        probe = await asyncio.create_subprocess_exec(
            "node", "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(probe.communicate(), timeout=5)
    except Exception:
        return {"success": False, "error": "Node.js is not installed or not on PATH"}

    t0 = _time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            "node", "-e", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_proc(proc)
            raise
        wall_ms = round((_time.perf_counter() - t0) * 1000, 1)
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if len(out) > _MAX_OUTPUT_CHARS:
            out = out[:_MAX_OUTPUT_CHARS] + "\n... [TRUNCATED]"
        log.info(f"[executor] js rc={proc.returncode} wall={wall_ms}ms")
        return {"success": proc.returncode == 0, "return_code": proc.returncode,
                "stdout": out, "stderr": err, "wall_ms": wall_ms, "language": "javascript"}
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def execute_bash(code: str, timeout: int = 15) -> dict:
    """Run a bash snippet. Only whitelisted commands allowed; dangerous patterns blocked."""
    if _BASH_BLOCKED.search(code):
        return {"success": False, "blocked": True,
                "reason": "Blocked: dangerous pattern in bash command"}

    # Every command must be whitelisted — not just the first token. Split on
    # newlines and shell separators (; && || |) so chained commands like
    # `echo ok && curl evil` can't smuggle a non-whitelisted command through.
    segments = re.split(r"(?:\n|;|\|\||&&|\|)", code)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        cmd = seg.split()[0]
        if cmd not in _BASH_WHITELIST:
            return {"success": False, "blocked": True,
                    "reason": f"Command not whitelisted: '{cmd}'. "
                              f"Allowed: {sorted(_BASH_WHITELIST)}"}

    t0 = _time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _kill_proc(proc)
            raise
        wall_ms = round((_time.perf_counter() - t0) * 1000, 1)
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if len(out) > _MAX_OUTPUT_CHARS:
            out = out[:_MAX_OUTPUT_CHARS] + "\n... [TRUNCATED]"
        log.info(f"[executor] bash rc={proc.returncode} wall={wall_ms}ms")
        return {"success": proc.returncode == 0, "return_code": proc.returncode,
                "stdout": out, "stderr": err, "wall_ms": wall_ms, "language": "bash"}
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Timed out after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}
