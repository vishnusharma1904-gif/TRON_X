"""
TRON-X Code Agent
──────────────────
Generate → Execute → Repair loop.
  1. LLM generates Python code for the task
  2. Sandbox executes it
  3. On failure, LLM repairs up to MAX_RETRIES times
  4. Returns final code + output
"""
from __future__ import annotations

import re
from src.core.logger import log

MAX_RETRIES = 3


def _extract_code(text: str) -> str:
    """Extract Python code from LLM response (strips markdown fences)."""
    # Try fenced block first
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Try inline code
    m = re.search(r"`([^`]+)`", text)
    if m:
        return m.group(1).strip()
    # Return raw text
    return text.strip()


class CodeAgent:
    """Generate and execute Python code with auto-repair."""

    async def run(
        self,
        task: str,
        persona: str = "jarvis",
        session_id: str = "__code_agent__",
    ) -> str:
        from src.intelligence.orchestrator import get_orchestrator
        from src.system.executor import execute_python

        orch = get_orchestrator()

        # 1. Generate initial code
        gen_prompt = (
            f"Write Python code to: {task}\n"
            "Return ONLY the code inside a ```python block. "
            "No explanations before or after. "
            "Keep it self-contained, no imports from custom modules."
        )
        result = await orch.chat(
            gen_prompt, session_id, "coding", persona, max_tokens=800
        )
        code = _extract_code(result.get("reply", ""))
        log.info(f"[code_agent] Generated {len(code)} chars of code")

        # 2. Execute + repair loop
        last_error = ""
        for attempt in range(MAX_RETRIES):
            exec_result = await execute_python(code, timeout=20)

            if exec_result.get("blocked"):
                return f"Code blocked: {exec_result['reason']}"

            if exec_result.get("success") or not exec_result.get("stderr"):
                output = exec_result.get("stdout", "").strip() or "(no output)"
                log.info(f"[code_agent] Succeeded on attempt {attempt + 1}")
                return (
                    f"**Code:**\n```python\n{code}\n```\n\n"
                    f"**Output:**\n```\n{output}\n```"
                )

            # Repair
            last_error = exec_result.get("stderr", "Unknown error")
            log.warning(f"[code_agent] Attempt {attempt + 1} failed: {last_error[:100]}")

            repair_prompt = (
                f"This Python code failed:\n```python\n{code}\n```\n"
                f"Error:\n```\n{last_error[:600]}\n```\n"
                f"Fix the code. Return ONLY the corrected code in a ```python block."
            )
            repair_result = await orch.chat(
                repair_prompt, session_id, "coding", persona, max_tokens=800
            )
            code = _extract_code(repair_result.get("reply", code))

        return (
            f"**Code (after {MAX_RETRIES} repair attempts):**\n```python\n{code}\n```\n\n"
            f"**Last error:**\n```\n{last_error[:500]}\n```"
        )
