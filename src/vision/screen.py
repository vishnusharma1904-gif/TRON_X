"""
TRON-X Screen Capture + OCR
----------------------------
Fast screen/region/window capture via mss.
OCR pipeline: pytesseract (fast) -> EasyOCR fallback (accurate).
Vision description: capture -> base64 -> VisionAgent.
All blocking I/O runs in executor.
"""
from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Optional

from src.core.logger import log

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dir(path: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _default_path(prefix: str = "screen") -> str:
    return f"memory/cache/{prefix}_{int(time.time())}.png"

def _to_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()

# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

async def capture_screen(
    save_path: Optional[str] = None,
    region: Optional[dict] = None,
    monitor: int = 1,
    return_base64: bool = False,
) -> dict:
    """
    Capture full screen or a region using mss.

    region: {"top": int, "left": int, "width": int, "height": int}
    monitor: 1-based monitor index (1 = primary).
    """
    def _snap():
        try:
            import mss
            from mss.tools import to_png
        except ImportError:
            return {"success": False, "error": "pip install mss"}

        path = _ensure_dir(save_path or _default_path())
        with mss.mss() as sct:
            if region:
                bbox = {
                    "top":    region.get("top", 0),
                    "left":   region.get("left", 0),
                    "width":  region.get("width", 800),
                    "height": region.get("height", 600),
                    "mon":    monitor,
                }
                img = sct.grab(bbox)
            else:
                img = sct.grab(sct.monitors[monitor])

            to_png(img.rgb, img.size, output=str(path))

        return {
            "success": True,
            "path": str(path),
            "width": img.size[0],
            "height": img.size[1],
        }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _snap)
    if result.get("success") and return_base64:
        result["base64"] = _to_base64(result["path"])
    log.info(f"[screen] capture -> {result.get('path')} {result.get('width')}x{result.get('height')}")
    return result


async def capture_window(
    title: str,
    save_path: Optional[str] = None,
    return_base64: bool = False,
) -> dict:
    """
    Capture a specific window by title substring (Windows only).
    Falls back to full-screen capture if window not found.
    """
    def _snap():
        try:
            import pygetwindow as gw
            wins = gw.getWindowsWithTitle(title)
        except ImportError:
            wins = []

        path = _ensure_dir(save_path or _default_path("window"))

        if wins:
            w = wins[0]
            region = {
                "top":    max(0, w.top),
                "left":   max(0, w.left),
                "width":  w.width,
                "height": w.height,
            }
            found = True
        else:
            region = None
            found = False

        try:
            import mss
            from mss.tools import to_png
        except ImportError:
            return {"success": False, "error": "pip install mss"}

        with mss.mss() as sct:
            if region:
                img = sct.grab(region)
            else:
                img = sct.grab(sct.monitors[1])
            to_png(img.rgb, img.size, output=str(path))

        return {
            "success": True,
            "path": str(path),
            "window_found": found,
            "window_title": wins[0].title if found else None,
            "width": img.size[0],
            "height": img.size[1],
        }

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _snap)
    if result.get("success") and return_base64:
        result["base64"] = _to_base64(result["path"])
    log.info(f"[screen] window '{title}' found={result.get('window_found')}")
    return result


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

async def ocr_image(
    path: str,
    engine: str = "auto",
) -> dict:
    """
    Run OCR on an image file.
    engine: "tesseract" | "easyocr" | "auto" (tesseract first, easyocr if empty/fails)
    """
    def _tesseract(p: str) -> tuple[str, float]:
        import pytesseract
        from PIL import Image
        img = Image.open(p)
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        text = pytesseract.image_to_string(img).strip()
        confs = [c for c in data["conf"] if isinstance(c, (int, float)) and c >= 0]
        avg_conf = round(sum(confs) / len(confs), 1) if confs else 0.0
        return text, avg_conf

    def _easyocr(p: str) -> tuple[str, float]:
        import easyocr
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        results = reader.readtext(p)
        text = "\n".join(r[1] for r in results)
        confs = [r[2] for r in results] if results else []
        avg_conf = round(sum(confs) / len(confs) * 100, 1) if confs else 0.0
        return text, avg_conf

    def _run():
        if not Path(path).exists():
            return {"success": False, "error": f"File not found: {path}"}

        if engine in ("tesseract", "auto"):
            try:
                text, conf = _tesseract(path)
                if text or engine == "tesseract":
                    return {"success": True, "text": text, "engine": "tesseract", "confidence": conf}
            except Exception as e:
                if engine == "tesseract":
                    return {"success": False, "error": f"Tesseract: {e}"}
                log.warning(f"[screen] Tesseract failed ({e}), trying EasyOCR")

        # easyocr path (explicit or auto-fallback)
        try:
            text, conf = _easyocr(path)
            return {"success": True, "text": text, "engine": "easyocr", "confidence": conf}
        except Exception as e:
            return {"success": False, "error": f"EasyOCR: {e}"}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run)
    if result.get("success"):
        log.info(f"[screen] OCR engine={result['engine']} conf={result['confidence']}% chars={len(result['text'])}")
    return result


async def ocr_screen(
    region: Optional[dict] = None,
    engine: str = "auto",
) -> dict:
    """Capture screen then OCR it. Convenience wrapper."""
    capture = await capture_screen(region=region)
    if not capture["success"]:
        return capture
    ocr = await ocr_image(capture["path"], engine=engine)
    return {**ocr, "screenshot_path": capture["path"],
            "width": capture["width"], "height": capture["height"]}


# ---------------------------------------------------------------------------
# Vision description
# ---------------------------------------------------------------------------

async def describe_screen(
    region: Optional[dict] = None,
    prompt: str = "Describe what you see on this screen in detail.",
    return_base64: bool = False,
) -> dict:
    """
    Capture screen -> send to VisionAgent -> return NL description.
    """
    capture = await capture_screen(region=region, return_base64=True)
    if not capture["success"]:
        return capture

    try:
        from src.agents.vision_agent import VisionAgent
        agent = VisionAgent()
        description = await agent.run(
            prompt=prompt,
            image_b64=capture["base64"],
            image_mime="image/png",
        )
    except Exception as e:
        description = f"Vision model unavailable: {e}"

    result = {
        "success": True,
        "description": description,
        "path": capture["path"],
        "width": capture["width"],
        "height": capture["height"],
    }
    if return_base64:
        result["base64"] = capture["base64"]
    log.info(f"[screen] describe -> {len(description)} chars")
    return result


async def describe_image(
    path: str,
    prompt: str = "Describe this image in detail.",
) -> dict:
    """Run VisionAgent on an existing image file."""
    if not Path(path).exists():
        return {"success": False, "error": f"File not found: {path}"}
    try:
        from src.agents.vision_agent import VisionAgent
        agent = VisionAgent()
        description = await agent.run(prompt=prompt, image_path=path)
        return {"success": True, "description": description, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}
