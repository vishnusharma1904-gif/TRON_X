"""
TRON-X CadQuery Agent
──────────────────────
Natural language → CadQuery Python → 3D model (STL/STEP).
Generate → Execute → Repair loop (same pattern as CodeAgent).
Falls back gracefully if cadquery is not installed.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.core.logger import log

MAX_RETRIES = 3

_CAD_SYSTEM_PROMPT = """\
You are a CadQuery expert. Generate Python code using the cadquery library.
Rules:
  - Import: import cadquery as cq
  - Build a model as: result = cq.Workplane("XY")...
  - Export at end: result.val().exportStl("output.stl")
  - Keep units in mm
  - Return ONLY code in a ```python block, no explanations
"""


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


class CADAgent:
    """Generate 3D models from natural language via CadQuery."""

    async def run(
        self,
        description: str,
        output_dir: str = "memory/cache/cad",
        persona: str = "jarvis",
        session_id: str = "__cad_agent__",
    ) -> str:
        # Check CadQuery availability
        try:
            import cadquery as cq  # noqa: F401
        except ImportError:
            return (
                "CadQuery is not installed. "
                "Install it with: pip install cadquery\n"
                "Then I can generate 3D models for you."
            )

        from src.intelligence.orchestrator import get_orchestrator
        from src.system.executor import execute_python

        orch = get_orchestrator()
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # 1. Generate CadQuery code
        gen_prompt = f"Create a 3D model: {description}"
        result = await orch.chat(
            gen_prompt, session_id, "cad", persona,
            max_tokens=1000,
            extra_system=_CAD_SYSTEM_PROMPT,
        )
        code = _extract_code(result.get("reply", ""))
        log.info(f"[cad_agent] Generated {len(code)} chars of CadQuery code")

        # 2. Execute + repair loop
        last_error = ""
        for attempt in range(MAX_RETRIES):
            exec_result = await execute_python(code, timeout=30, allow_network=False)

            if exec_result.get("success") and not exec_result.get("stderr"):
                stl_path = Path(output_dir) / "output.stl"
                log.info(f"[cad_agent] STL exported: {stl_path}")
                return (
                    f"**3D Model Generated:**\n"
                    f"Description: {description}\n"
                    f"File: `{stl_path}`\n\n"
                    f"**Code:**\n```python\n{code}\n```"
                )

            last_error = exec_result.get("stderr", "Unknown error")
            log.warning(f"[cad_agent] Attempt {attempt + 1} failed: {last_error[:100]}")

            repair_prompt = (
                f"This CadQuery code failed:\n```python\n{code}\n```\n"
                f"Error:\n```\n{last_error[:600]}\n```\n"
                "Fix it. Return ONLY corrected code in a ```python block."
            )
            repair = await orch.chat(
                repair_prompt, session_id, "cad", persona, max_tokens=1000
            )
            code = _extract_code(repair.get("reply", code))

        return (
            f"**CadQuery code (failed after {MAX_RETRIES} attempts):**\n"
            f"```python\n{code}\n```\n\n"
            f"**Last error:**\n```\n{last_error[:500]}\n```"
        )
