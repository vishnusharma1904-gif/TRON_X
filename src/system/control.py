"""
TRON-X OS Control
------------------
Cross-platform system control: volume, brightness, apps, screenshots.
Uses pyautogui + platform-specific subprocess calls.
All actions are async-safe and logged.
"""
from __future__ import annotations
import asyncio
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional
from src.core.logger import log

OS = platform.system()  # "Windows" | "Darwin" | "Linux"

async def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    """Run subprocess async, return (returncode, stdout+stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (stdout + stderr).decode(errors="replace").strip()
        return proc.returncode, out
    except asyncio.TimeoutError:
        return -1, "Command timed out"
    except Exception as e:
        return -1, str(e)

async def set_volume(level: int) -> dict:
    """Set system volume 0-100."""
    level = max(0, min(100, level))
    if OS == "Windows":
        code, out = await _run([
            "powershell", "-Command",
            f"(New-Object -ComObject WScript.Shell).SendKeys([char]174 * 50);"
            f"$up = [Math]::Round({level} / 2);"
            f"(New-Object -ComObject WScript.Shell).SendKeys([char]175 * $up)"
        ])
    elif OS == "Darwin":
        code, out = await _run(["osascript", "-e", f"set volume output volume {level}"])
    else:
        code, out = await _run(["amixer", "-q", "sset", "Master", f"{level}%"])
    log.info(f"[system] Volume -> {level}% (rc={code})")
    return {"action": "volume", "level": level, "success": code == 0}

async def mute() -> dict:
    if OS == "Windows":
        code, _ = await _run(["powershell", "-Command",
            "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"])
    elif OS == "Darwin":
        code, _ = await _run(["osascript", "-e", "set volume output muted true"])
    else:
        code, _ = await _run(["amixer", "-q", "sset", "Master", "mute"])
    return {"action": "mute", "success": code == 0}

async def set_brightness(level: int) -> dict:
    """Set screen brightness 0-100."""
    level = max(0, min(100, level))
    if OS == "Windows":
        code, out = await _run([
            "powershell", "-Command",
            f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)"
            f".WmiSetBrightness(1,{level})"
        ])
    elif OS == "Darwin":
        code, out = await _run(["brightness", str(level / 100)])
    else:
        code, out = await _run(["xrandr", "--output", "eDP-1", "--brightness",
                                 str(level / 100)])
    log.info(f"[system] Brightness -> {level}% (rc={code})")
    return {"action": "brightness", "level": level, "success": code == 0}

_APP_MAP_WINDOWS = {
    "chrome":        "chrome.exe",
    "firefox":       "firefox.exe",
    "notepad":       "notepad.exe",
    "explorer":      "explorer.exe",
    "terminal":      "wt.exe",
    "cmd":           "cmd.exe",
    "calculator":    "calc.exe",
    "paint":         "mspaint.exe",
    "task manager":  "taskmgr.exe",
    "vscode":        "code",
    "spotify":       "spotify.exe",
}

_APP_MAP_MAC = {
    "chrome":        "Google Chrome",
    "firefox":       "Firefox",
    "terminal":      "Terminal",
    "calculator":    "Calculator",
    "vscode":        "Visual Studio Code",
    "spotify":       "Spotify",
}

async def open_app(app_name: str) -> dict:
    """Open an application by name."""
    name = app_name.lower().strip()
    if OS == "Windows":
        exe = _APP_MAP_WINDOWS.get(name, name)
        code, out = await _run(["cmd", "/c", "start", "", exe])
    elif OS == "Darwin":
        app = _APP_MAP_MAC.get(name, app_name)
        code, out = await _run(["open", "-a", app])
    else:
        code, out = await _run(["xdg-open", name])
    log.info(f"[system] Open '{app_name}' -> rc={code}")
    return {"action": "open_app", "app": app_name, "success": code == 0, "detail": out}

async def close_app(app_name: str) -> dict:
    """Close/kill an application by name."""
    name = app_name.lower().strip()
    if OS == "Windows":
        exe = _APP_MAP_WINDOWS.get(name, name)
        if not exe.endswith(".exe"):
            exe += ".exe"
        code, out = await _run(["taskkill", "/IM", exe, "/F"])
    elif OS == "Darwin":
        code, out = await _run(["pkill", "-x", app_name])
    else:
        code, out = await _run(["pkill", "-f", app_name])
    log.info(f"[system] Close '{app_name}' -> rc={code}")
    return {"action": "close_app", "app": app_name, "success": code == 0}

async def take_screenshot(save_path: Optional[str] = None) -> dict:
    """Take a screenshot and save to path (or temp). Returns file path."""
    try:
        import pyautogui
        import time as _time
        path = Path(save_path) if save_path else Path(f"memory/cache/screenshot_{int(_time.time())}.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        img = await loop.run_in_executor(None, pyautogui.screenshot)
        img.save(str(path))
        log.info(f"[system] Screenshot saved -> {path}")
        return {"action": "screenshot", "path": str(path), "success": True}
    except ImportError:
        return {"action": "screenshot", "success": False, "error": "pip install pyautogui pillow"}
    except Exception as e:
        return {"action": "screenshot", "success": False, "error": str(e)}

async def get_system_info() -> dict:
    """Return CPU, RAM, disk usage."""
    try:
        import psutil
        return {
            "cpu_percent":   psutil.cpu_percent(interval=0.5),
            "ram_total_gb":  round(psutil.virtual_memory().total / 1e9, 1),
            "ram_used_pct":  psutil.virtual_memory().percent,
            "disk_used_pct": psutil.disk_usage("/").percent,
            "os":            OS,
        }
    except ImportError:
        return {"error": "pip install psutil"}

# =============================================================================
# Phase 4 additions -- process & service management
# =============================================================================

async def list_processes(sort_by: str = "cpu") -> dict:
    try:
        import psutil
    except ImportError:
        return {"error": "pip install psutil"}

    def fetch_processes():
        psutil.cpu_percent(interval=0.5)
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
            try:
                info = p.info
                if info["name"] is None:
                    continue
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"],
                    "cpu_pct": info["cpu_percent"] or 0.0,
                    "mem_pct": info["memory_percent"] or 0.0,
                    "status": info["status"] or "unknown",
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return procs

    loop = asyncio.get_event_loop()
    procs = await loop.run_in_executor(None, fetch_processes)
    total_count = len(procs)
    if sort_by == "cpu":
        procs.sort(key=lambda x: x["cpu_pct"], reverse=True)
    elif sort_by == "memory":
        procs.sort(key=lambda x: x["mem_pct"], reverse=True)
    return {"processes": procs[:30], "total_count": total_count, "sort_by": sort_by}

async def kill_process(identifier: str | int) -> dict:
    try:
        import psutil
    except ImportError:
        return {"success": False, "error": "pip install psutil"}

    _PROTECTED = {"system", "smss.exe", "csrss.exe", "wininit.exe",
                  "winlogon.exe", "lsass.exe", "svchost.exe"}

    def do_kill():
        try:
            if str(identifier).isdigit():
                pid = int(identifier)
                p = psutil.Process(pid)
                if p.name().lower() in _PROTECTED:
                    return (False, "Cannot kill protected process")
                p.kill()
                return (True, [pid])
            else:
                target = str(identifier).lower()
                if target in _PROTECTED:
                    return (False, "Cannot kill protected process")
                killed = []
                for p in psutil.process_iter(["pid", "name"]):
                    try:
                        name = p.info.get("name")
                        if name and name.lower() == target:
                            p.kill()
                            killed.append(p.info["pid"])
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        continue
                return (True, killed)
        except Exception as e:
            return (False, str(e))

    loop = asyncio.get_event_loop()
    success, result = await loop.run_in_executor(None, do_kill)
    if success:
        return {"success": True, "killed": result, "identifier": str(identifier)}
    else:
        return {"success": False, "error": result, "identifier": str(identifier)}

async def start_process(path_or_name: str) -> dict:
    target = _APP_MAP_WINDOWS.get(path_or_name.lower(), path_or_name)

    def do_start():
        try:
            proc = subprocess.Popen([target], shell=False)
            return (True, proc.pid)
        except Exception as e:
            return (False, str(e))

    loop = asyncio.get_event_loop()
    success, result = await loop.run_in_executor(None, do_start)
    if success:
        return {"success": True, "pid": result, "launched": path_or_name}
    else:
        return {"success": False, "error": result}

async def list_services(state_filter: str = "all") -> dict:
    if OS != "Windows":
        return {"error": "Windows only"}

    rc, out = await _run([
        "powershell", "-NoProfile", "-Command",
        "Get-Service | Select-Object Name,DisplayName,Status | ConvertTo-Json -Compress -Depth 1 -AsArray"
    ])
    try:
        import json
        data = json.loads(out)
    except Exception:
        return {"services": [], "count": 0, "filter": state_filter}

    services = []
    for s in data:
        status_val = s.get("Status", 0)
        status_str = "running" if status_val == 4 else "stopped"
        services.append({
            "name": s.get("Name", ""),
            "display_name": s.get("DisplayName", ""),
            "status": status_str,
        })
    if state_filter in ("running", "stopped"):
        services = [s for s in services if s["status"] == state_filter]
    return {"services": services, "count": len(services), "filter": state_filter}

async def service_action(service_name: str, action: str) -> dict:
    if OS != "Windows":
        return {"success": False, "error": "Windows only"}
    if action.lower() not in {"start", "stop", "restart"}:
        return {"success": False, "error": "Invalid action"}

    _CRITICAL = {"windefend", "eventlog", "wuauserv", "lanmanserver"}
    if service_name.lower() in _CRITICAL:
        return {"success": False, "error": "Cannot modify critical service"}

    rc, out = await _run([
        "powershell", "-NoProfile", "-Command",
        f"{action.capitalize()}-Service -Name '{service_name}'"
    ])
    return {"success": rc == 0, "service": service_name, "action": action.lower(), "output": out}
