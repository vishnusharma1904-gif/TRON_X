from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import Path
from asyncio.subprocess import PIPE
from src.core.logger import log

CMD_WHITELIST: list[str] = [
    "Get-Process", "Get-Service", "Get-WmiObject", "Get-CimInstance",
    "Get-ChildItem", "Get-Item", "Get-Content", "Get-Disk",
    "Get-Volume", "Get-NetAdapter", "Get-NetIPAddress",
    "Get-EventLog", "Get-Date", "Get-Uptime", "Get-ComputerInfo",
    "Measure-Object", "Select-Object", "Where-Object", "Sort-Object",
    "Format-List", "Format-Table", "Out-String",
    "Test-Path", "Test-Connection", "Resolve-DnsName",
    "tasklist", "systeminfo", "ipconfig", "netstat", "ping",
    "wmic process", "wmic service"
]

BLOCKED_PATTERNS: list[str] = [
    r"rm\s+-r", 
    r"remove-item.*-recurse", 
    r"del\s+/[sf]",
    r"format\s+[a-z]:",
    r"reg\s+(add|delete|import)",
    r"net\s+user", 
    r"net\s+localgroup",
    r"Set-ExecutionPolicy",
    r"Invoke-Expression", 
    r"iex\s",
    r"IEX\s",
    r"\|\s*powershell", 
    r"\|\s*cmd",
    r"Start-Process.*-Verb.*RunAs",
    r"New-Service", 
    r"sc\s+create",
    r"curl\s+.*\|\s*(bash|sh|powershell)",
    r"FromBase64String"
]

async def safety_scan(command: str) -> tuple[bool, str]:
    cmd_lower = command.lower()
    
    for pattern in BLOCKED_PATTERNS:
        # Using re.IGNORECASE to ensure patterns catch all variations 
        # even though we included exact casing in the list per instructions
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Blocked: Matches restricted pattern '{pattern}'"
            
    is_whitelisted = False
    for prefix in CMD_WHITELIST:
        if cmd_lower.startswith(prefix.lower()):
            is_whitelisted = True
            break
            
    if not is_whitelisted:
        return False, "Command not in whitelist"
        
    return True, "ok"

async def run_powershell(command: str, timeout: int = 15) -> dict:
    is_safe, reason = await safety_scan(command)
    if not is_safe:
        log.warning(f"PowerShell execution blocked: {reason}")
        return {"success": False, "blocked": True, "reason": reason, "output": ""}
        
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell", 
            "-NoProfile", 
            "-NonInteractive", 
            "-Command", 
            command,
            stdout=PIPE, 
            stderr=PIPE
        )
        
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
        
        raw_output = (stdout.decode('utf-8', errors='replace') + stderr.decode('utf-8', errors='replace')).strip()
        output = raw_output[:4000]
        
        log.info(f"Executed PowerShell command: '{command}' | ReturnCode: {rc}")
        return {
            "success": rc == 0,
            "returncode": rc,
            "output": output,
            "command": command,
            "blocked": False
        }
        
    except asyncio.TimeoutError:
        log.error(f"PowerShell execution timed out for: '{command}'")
        return {
            "success": False,
            "timeout": True,
            "output": "Command timed out"
        }
    except Exception as e:
        log.error(f"Error executing PowerShell: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }

async def nl_to_powershell(query: str, router) -> dict:
    messages = [
        {"role": "system", "content": "You are a Windows PowerShell expert. Convert the user's request into a single safe PowerShell command. Respond with ONLY the command, no explanation, no markdown, no code block."},
        {"role": "user", "content": query}
    ]
    
    response, model_used = await router.complete(messages=messages, category="fast_chat")
    raw_content = response.choices[0].message.content.strip()
    
    clean_command = re.sub(r"^```powershell\s*", "", raw_content, flags=re.IGNORECASE)
    clean_command = re.sub(r"^```.*\s*", "", clean_command)
    clean_command = re.sub(r"\s*```$", "", clean_command)
    clean_command = clean_command.strip()
    
    result = await run_powershell(clean_command)
    result["nl_query"] = query
    result["model"] = model_used
    
    return result