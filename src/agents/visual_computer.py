"""
TRON-X Visual Computer Agent  (Phase 4)
─────────────────────────────────────────
AI-guided "see → think → act" loop for desktop automation.

Pipeline per step:
  1. Take screenshot via ComputerAgent
  2. Send screenshot + instruction to vision LLM
  3. LLM returns structured action JSON (type + coordinates/text)
  4. Validate and execute the action
  5. Take post-action screenshot
  6. LLM decides: done | next_step

SSE streaming:
  Each step yields a dict event:
    {type: "computer_step", step, action, description, screenshot?, success, done}

Public API:
    agent = get_visual_computer()
    async for event in agent.stream(instruction, persona, max_steps):
        ...
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator

from src.core.logger import log
from src.agents.computer_agent import get_computer_agent, ActionResult

# ─────────────────────────────────────────────────────────────────────────────
# Vision prompt
# ─────────────────────────────────────────────────────────────────────────────

_VISION_ACTION_PROMPT = """You are a computer control AI. You are looking at a screenshot of a computer screen.

User instruction: {instruction}

Screen resolution: {width}x{height}
Previous steps taken: {steps_summary}

Analyse the screenshot carefully and decide the SINGLE NEXT action to take.

Respond ONLY with valid JSON in this exact format:
{{
  "action": "<action_type>",
  "params": {{ ... }},
  "description": "<one sentence describing what you're doing>",
  "done": false,
  "reasoning": "<brief reasoning>"
}}

Available action types:
- click: {{"x": int, "y": int, "button": "left"|"right"}}
- double_click: {{"x": int, "y": int}}
- type_text: {{"text": "string to type"}}
- press_key: {{"keys": "key or combo like ctrl+c, enter, ctrl+a"}}
- scroll: {{"x": int, "y": int, "direction": "up"|"down", "clicks": int}}
- open_url: {{"url": "https://..."}}
- drag: {{"x1": int, "y1": int, "x2": int, "y2": int}}
- wait: {{"seconds": float}}
- done: {{"result": "description of what was accomplished"}}

Rules:
- Click coordinates must be within screen bounds ({width}x{height})
- If the task is complete, use action "done"
- If you cannot determine next action, use action "done" with result explaining why
- Be precise with coordinates — click the center of buttons/links/fields
- For typing: first click the field, then type_text
"""

_VERIFY_PROMPT = """You previously took this action: {action_description}

Looking at the CURRENT screenshot after the action:
- Did the action succeed? (y/n)
- Is the overall task complete? (y/n)

Respond with JSON: {{"success": true/false, "task_done": true/false, "observation": "brief note"}}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Step record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ComputerStep:
    step_num:    int
    action:      str
    params:      dict
    description: str
    reasoning:   str    = ""
    success:     bool   = False
    error:       str    = ""
    screenshot_before: str = ""   # base64
    screenshot_after:  str = ""   # base64
    latency_ms:  int    = 0


# ─────────────────────────────────────────────────────────────────────────────
# VisualComputerAgent
# ─────────────────────────────────────────────────────────────────────────────

class VisualComputerAgent:
    """AI-guided desktop automation using vision model + ComputerAgent."""

    MAX_STEPS = 8

    def __init__(self) -> None:
        self._ca = get_computer_agent()

    def _build_steps_summary(self, steps: list[ComputerStep]) -> str:
        if not steps:
            return "None yet."
        lines = []
        for s in steps:
            status = "✓" if s.success else "✗"
            lines.append(f"Step {s.step_num}: [{status}] {s.description}")
        return "\n".join(lines)

    def _parse_action_json(self, raw: str) -> dict | None:
        """Extract JSON from LLM response (may have markdown code fences)."""
        # Strip markdown fences
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip()
        # Find first { ... }
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None

    async def _llm_plan_action(
        self,
        instruction: str,
        screenshot_b64: str,
        steps: list[ComputerStep],
        screen_w: int,
        screen_h: int,
        router,
        persona: str,
    ) -> dict | None:
        """Ask the vision model what action to take next."""
        from src.intelligence.persona import PersonaEngine
        pe = PersonaEngine()
        style = pe.get_persona_style_note(persona)

        system_content = (
            f"You are TRON-X ({persona.upper()}) — a precise computer control AI. {style}\n"
            "Respond only with valid JSON — no markdown, no prose."
        )

        user_content = [
            {
                "type": "text",
                "text": _VISION_ACTION_PROMPT.format(
                    instruction=instruction,
                    width=screen_w,
                    height=screen_h,
                    steps_summary=self._build_steps_summary(steps),
                ),
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{screenshot_b64}",
                    "detail": "high",
                },
            },
        ]

        try:
            response, _ = await router.complete(
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user",   "content": user_content},
                ],
                category="vision",
                temperature=0.1,
                max_tokens=400,
            )
            raw = response.choices[0].message.content or ""
            parsed = self._parse_action_json(raw)
            if parsed:
                return parsed
            log.warning(f"[visual_computer] Could not parse LLM response: {raw[:200]}")
            return None
        except Exception as e:
            log.error(f"[visual_computer] Vision LLM failed: {e}")
            return None

    async def _execute_planned_action(self, action_dict: dict) -> tuple[ActionResult, bool]:
        """Execute the action dict returned by the vision LLM. Returns (result, is_done)."""
        action_type = action_dict.get("action", "done")
        params      = action_dict.get("params", {})

        if action_type == "done":
            return ActionResult(success=True, action="done", details=params), True

        if action_type == "wait":
            secs = float(params.get("seconds", 0.5))
            await asyncio.sleep(min(secs, 5.0))
            return ActionResult(success=True, action="wait", details=params), False

        result = await self._ca.execute(action_type, **params)
        # Give UI time to react
        await asyncio.sleep(0.4)
        return result, False

    # ── Main streaming entry point ────────────────────────────────────────────

    async def stream(
        self,
        instruction:  str,
        persona:      str  = "jarvis",
        max_steps:    int  = MAX_STEPS,
        router        = None,
        capture_each: bool = True,
    ) -> AsyncGenerator[dict, None]:
        """
        AI-guided automation stream.

        Yields dicts:
          {type: "computer_start",  instruction, available}
          {type: "computer_step",   step, action, description, screenshot, success, error, done}
          {type: "computer_done",   steps_taken, result_description, latency_ms}
          {type: "error",           message}
        """
        if router is None:
            from src.intelligence.router import get_router
            router = get_router()

        t0 = time.monotonic()

        if not self._ca.is_available:
            yield {
                "type":        "computer_start",
                "instruction": instruction,
                "available":   False,
            }
            yield {
                "type":    "error",
                "message": (
                    "Computer control unavailable. "
                    "Install: pip install pyautogui mss Pillow --break-system-packages"
                ),
            }
            return

        # Get screen dimensions
        screen_info = await self._ca.get_screen_size()
        screen_w    = screen_info.width
        screen_h    = screen_info.height

        yield {
            "type":        "computer_start",
            "instruction": instruction,
            "available":   True,
            "screen":      {"width": screen_w, "height": screen_h},
        }

        steps: list[ComputerStep] = []
        overall_done   = False
        final_result   = ""

        for step_num in range(1, max_steps + 1):
            step_t0 = time.monotonic()

            # 1. Take screenshot before
            ss_before = await self._ca.screenshot_fast()

            yield {
                "type":       "computer_step",
                "step":       step_num,
                "phase":      "analysing",
                "message":    f"Analysing screen (step {step_num}/{max_steps})…",
                "screenshot": ss_before,
            }

            # 2. Ask vision LLM for next action
            action_dict = await self._llm_plan_action(
                instruction=instruction,
                screenshot_b64=ss_before,
                steps=steps,
                screen_w=screen_w,
                screen_h=screen_h,
                router=router,
                persona=persona,
            )

            if action_dict is None:
                yield {"type": "error", "message": "Vision model failed to plan action"}
                break

            action_type = action_dict.get("action", "done")
            description = action_dict.get("description", action_type)
            reasoning   = action_dict.get("reasoning", "")

            if action_type == "done":
                final_result = action_dict.get("params", {}).get("result", "Task completed.")
                overall_done = True
                steps.append(ComputerStep(
                    step_num=step_num, action="done", params={},
                    description=description, reasoning=reasoning,
                    success=True, latency_ms=int((time.monotonic() - step_t0) * 1000),
                ))
                yield {
                    "type":        "computer_step",
                    "step":        step_num,
                    "phase":       "done",
                    "action":      "done",
                    "description": description,
                    "success":     True,
                    "done":        True,
                    "screenshot":  ss_before,
                }
                break

            yield {
                "type":        "computer_step",
                "step":        step_num,
                "phase":       "executing",
                "action":      action_type,
                "description": description,
                "reasoning":   reasoning,
                "screenshot":  ss_before,
                "done":        False,
            }

            # 3. Execute the action
            result, is_done = await self._execute_planned_action(action_dict)

            # 4. Take screenshot after (for HUD)
            ss_after = await self._ca.screenshot_fast() if capture_each else ""

            step = ComputerStep(
                step_num=step_num,
                action=action_type,
                params=action_dict.get("params", {}),
                description=description,
                reasoning=reasoning,
                success=result.success,
                error=result.error or "",
                screenshot_before=ss_before,
                screenshot_after=ss_after,
                latency_ms=int((time.monotonic() - step_t0) * 1000),
            )
            steps.append(step)

            yield {
                "type":        "computer_step",
                "step":        step_num,
                "phase":       "result",
                "action":      action_type,
                "description": description,
                "success":     result.success,
                "error":       result.error,
                "screenshot":  ss_after or ss_before,
                "done":        is_done,
            }

            if is_done or not result.success:
                overall_done = is_done
                if not result.success:
                    final_result = f"Action failed: {result.error}"
                break

        total_ms = int((time.monotonic() - t0) * 1000)
        ss_final = await self._ca.screenshot_fast()

        yield {
            "type":        "computer_done",
            "steps_taken": len(steps),
            "success":     overall_done,
            "result":      final_result or ("Completed" if overall_done else "Stopped"),
            "screenshot":  ss_final,
            "latency_ms":  total_ms,
        }

    async def single_action(
        self,
        instruction: str,
        persona:     str = "jarvis",
        router       = None,
    ) -> dict:
        """Non-streaming: run up to MAX_STEPS, return final state."""
        steps = []
        final = {}
        async for event in self.stream(instruction, persona=persona,
                                       router=router, max_steps=self.MAX_STEPS):
            if event["type"] == "computer_done":
                final = event
            steps.append(event)
        return {"events": steps, "result": final}


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: VisualComputerAgent | None = None


def get_visual_computer() -> VisualComputerAgent:
    global _instance
    if _instance is None:
        _instance = VisualComputerAgent()
    return _instance
