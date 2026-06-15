"""
TRON-X Vision Agent
────────────────────
Multimodal image analysis via vision-capable LLM models.
Accepts base64-encoded images or file paths.
Routes to vision model category (GPT-4o / Gemini Vision / OpenRouter).
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from src.core.logger import log


def _encode_image(path: str) -> tuple[str, str]:
    """Return (base64_data, mime_type) for an image file."""
    p = Path(path)
    suffix = p.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".gif": "image/gif",
                ".webp": "image/webp"}
    mime = mime_map.get(suffix, "image/png")
    data = base64.b64encode(p.read_bytes()).decode("utf-8")
    return data, mime


class VisionAgent:
    """
    Analyse images using the vision model category.
    Accepts:
      - file path  →  encoded on-the-fly
      - base64 str →  used directly
    """

    async def run(
        self,
        prompt: str,
        image_path: Optional[str] = None,
        image_b64: Optional[str] = None,
        image_mime: str = "image/png",
        persona: str = "jarvis",
        session_id: str = "__vision_agent__",
    ) -> str:
        """
        Analyse an image with a text prompt.
        Returns the LLM's description / analysis.
        """
        if not image_path and not image_b64:
            return "No image provided. Pass image_path or image_b64."

        # Encode file if path given
        if image_path and not image_b64:
            try:
                image_b64, image_mime = _encode_image(image_path)
                log.info(f"[vision] Encoded image: {image_path}")
            except Exception as e:
                return f"Could not read image file: {e}"

        # Build OpenAI-style image content block
        image_data = [{
            "type": "image_url",
            "image_url": {
                "url": f"data:{image_mime};base64,{image_b64}",
                "detail": "high",
            },
        }]

        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        result = await orch.chat(
            user_message=prompt,
            session_id=session_id,
            intent="vision",
            persona=persona,
            max_tokens=1000,
            image_data=image_data,
        )
        return result.get("reply", "Vision analysis failed.")

    async def describe(self, image_path: str, persona: str = "jarvis") -> str:
        """Describe what's in an image."""
        return await self.run(
            "Describe this image in detail. What do you see?",
            image_path=image_path,
            persona=persona,
        )

    async def extract_text(self, image_path: str) -> str:
        """Extract all readable text from an image (OCR-style)."""
        return await self.run(
            "Extract and transcribe all visible text from this image exactly as it appears.",
            image_path=image_path,
        )

    async def analyse_chart(self, image_path: str, persona: str = "jarvis") -> str:
        """Analyse a chart or graph image."""
        return await self.run(
            "Analyse this chart or graph. Describe the data, trends, and key insights.",
            image_path=image_path,
            persona=persona,
        )
