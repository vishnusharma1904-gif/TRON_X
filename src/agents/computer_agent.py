"""
TRON-X Computer Agent  (Phase 4)
──────────────────────────────────
Real desktop control: mouse, keyboard, screen capture.
Uses pyautogui for input and mss for fast screen capture.
All heavy operations run in a thread-pool to avoid blocking uvicorn.

Public API:
    agent = get_computer_agent()
    b64   = await agent.screenshot()               # base64 JPEG
    await agent.click(x, y)
    await agent.type_text("hello world")
    await agent.press_key("ctrl+c")
    await agent.scroll(x, y, clicks=3)
    await agent.open_url("https://google.com")
    info  = await agent.get_screen_size()
"""
from __future__ import annotations

import asyncio
import base64
import io
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from typing import Any

from src.core.logger import log

# ─────────────────────────────────────────────────────────────────────────────
# Lazy imports — pyautogui / mss are optional; graceful fallback if absent
# ─────────────────────────────────────────────────────────────────────────────

def _import_pyautogui():
    try:
        import pyautogui as pg
        pg.FAILSAFE   = True   # move mouse to top-left corner to abort
        pg.PAUSE      = 0.05   # small pause between actions
        return pg
    except ImportError:
        return None

def _import_mss():
    try:
        import mss
        return mss
    except ImportError:
        return None

def _import_pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ActionResult:
    success:     bool
    action:      str
    details:     dict = field(default_factory=dict)
    screenshot:  str | None = None   # base64 JPEG after action (if requested)
    error:       str | None = None
    latency_ms:  int = 0


@dataclass
class ScreenInfo:
    width:  int
    height: int
    monitors: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# ComputerAgent
# ─────────────────────────────────────────────────────────────────────────────

class ComputerAgent:
    """
    Wraps pyautogui + mss for async desktop control.
    All blocking calls are dispatched to a thread-pool executor.
    """

    JPEG_QUALITY = 55       # lower = smaller frames for SSE streaming
    FULL_QUALITY = 80       # for single screenshot requests

    def __init__(self) -> None:
        self._pg    = _import_pyautogui()
        self._mss   = _import_mss()
        self._Image = _import_pil()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._available = bool(self._pg and self._mss and self._Image)

        if not self._available:
            missing = []
            if not self._pg:    missing.append("pyautogui")
            if not self._mss:   missing.append("mss")
            if not self._Image: missing.append("Pillow")
            log.warning(
                f"[computer] Missing packages: {missing}. "
                "Install: pip install pyautogui mss Pillow --break-system-packages"
            )

    def _ensure_loop(self):
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

    async def _run(self, fn, *args, **kwargs):
        """Run a synchronous function in a thread-pool executor."""
        self._ensure_loop()
        return await self._loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    # ── Screen capture ────────────────────────────────────────────────────────

    def _take_screenshot_sync(self, quality: int = JPEG_QUALITY) -> str:
        """Capture screen → JPEG → base64 string. Synchronous."""
        with self._mss.mss() as sct:
            # Capture primary monitor
            monitor = sct.monitors[1]  # index 0 = all monitors, 1 = primary
            sct_img = sct.grab(monitor)

        # Convert to PIL Image
        img = self._Image.frombytes(
            "RGB",
            (sct_img.width, sct_img.height),
            sct_img.rgb,
        )

        # Encode as JPEG
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    async def screenshot(self, quality: int = FULL_QUALITY) -> str:
        """Return base64 JPEG screenshot of the primary monitor."""
        if not self._available:
            return ""
        try:
            return await self._run(self._take_screenshot_sync, quality)
        except Exception as e:
            log.error(f"[computer] screenshot error: {e}")
            return ""

    async def screenshot_fast(self) -> str:
        """Lower-quality screenshot optimised for streaming."""
        return await self.screenshot(quality=self.JPEG_QUALITY)

    # ── Screen info ───────────────────────────────────────────────────────────

    async def get_screen_size(self) -> ScreenInfo:
        if not self._available:
            return ScreenInfo(width=1920, height=1080)
        def _sync():
            with self._mss.mss() as sct:
                monitors = [dict(m) for m in sct.monitors]
            primary = monitors[1] if len(monitors) > 1 else monitors[0]
            return ScreenInfo(
                width=primary["width"],
                height=primary["height"],
                monitors=monitors,
            )
        return await self._run(_sync)

    # ── Mouse actions ─────────────────────────────────────────────────────────

    async def click(self, x: int, y: int, button: str = "left", capture_after: bool = False) -> ActionResult:
        if not self._available:
            return ActionResult(success=False, action="click", error="pyautogui not available")
        t0 = time.monotonic()
        try:
            def _sync():
                self._pg.click(x, y, button=button)
                time.sleep(0.1)
            await self._run(_sync)
            b64 = await self.screenshot_fast() if capture_after else None
            return ActionResult(
                success=True, action="click",
                details={"x": x, "y": y, "button": button},
                screenshot=b64,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="click", error=str(e))

    async def double_click(self, x: int, y: int) -> ActionResult:
        if not self._available:
            return ActionResult(success=False, action="double_click", error="unavailable")
        t0 = time.monotonic()
        try:
            await self._run(self._pg.doubleClick, x, y)
            return ActionResult(
                success=True, action="double_click",
                details={"x": x, "y": y},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="double_click", error=str(e))

    async def right_click(self, x: int, y: int) -> ActionResult:
        return await self.click(x, y, button="right")

    async def move_to(self, x: int, y: int, duration: float = 0.3) -> ActionResult:
        if not self._available:
            return ActionResult(success=False, action="move_to", error="unavailable")
        try:
            await self._run(self._pg.moveTo, x, y, duration)
            return ActionResult(success=True, action="move_to", details={"x": x, "y": y})
        except Exception as e:
            return ActionResult(success=False, action="move_to", error=str(e))

    async def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> ActionResult:
        if not self._available:
            return ActionResult(success=False, action="drag", error="unavailable")
        t0 = time.monotonic()
        try:
            def _sync():
                self._pg.moveTo(x1, y1, duration=0.2)
                self._pg.dragTo(x2, y2, duration=duration, button="left")
            await self._run(_sync)
            return ActionResult(
                success=True, action="drag",
                details={"from": [x1, y1], "to": [x2, y2]},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="drag", error=str(e))

    async def scroll(self, x: int, y: int, clicks: int = 3, direction: str = "down") -> ActionResult:
        if not self._available:
            return ActionResult(success=False, action="scroll", error="unavailable")
        t0 = time.monotonic()
        try:
            amount = -abs(clicks) if direction == "down" else abs(clicks)
            def _sync():
                self._pg.moveTo(x, y)
                self._pg.scroll(amount)
            await self._run(_sync)
            return ActionResult(
                success=True, action="scroll",
                details={"x": x, "y": y, "direction": direction, "clicks": clicks},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="scroll", error=str(e))

    # ── Keyboard actions ──────────────────────────────────────────────────────

    async def type_text(self, text: str, interval: float = 0.03) -> ActionResult:
        """Type text at the current cursor position."""
        if not self._available:
            return ActionResult(success=False, action="type_text", error="unavailable")
        t0 = time.monotonic()
        try:
            await self._run(self._pg.write, text, interval)
            return ActionResult(
                success=True, action="type_text",
                details={"text": text[:40] + ("…" if len(text) > 40 else "")},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="type_text", error=str(e))

    async def press_key(self, keys: str) -> ActionResult:
        """
        Press key(s). Single: 'enter'. Combo: 'ctrl+c'. Sequence: 'ctrl+a,ctrl+c'.
        """
        if not self._available:
            return ActionResult(success=False, action="press_key", error="unavailable")
        t0 = time.monotonic()
        try:
            def _sync():
                # Support comma-separated sequences: 'ctrl+a,ctrl+c'
                for combo in keys.split(","):
                    combo = combo.strip()
                    if "+" in combo:
                        parts = [p.strip() for p in combo.split("+")]
                        self._pg.hotkey(*parts)
                    else:
                        self._pg.press(combo)
                    time.sleep(0.05)
            await self._run(_sync)
            return ActionResult(
                success=True, action="press_key",
                details={"keys": keys},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="press_key", error=str(e))

    async def hotkey(self, *keys) -> ActionResult:
        """Press a keyboard shortcut, e.g. hotkey('ctrl', 'c')."""
        if not self._available:
            return ActionResult(success=False, action="hotkey", error="unavailable")
        try:
            await self._run(self._pg.hotkey, *keys)
            return ActionResult(success=True, action="hotkey", details={"keys": list(keys)})
        except Exception as e:
            return ActionResult(success=False, action="hotkey", error=str(e))

    # ── App / URL control ─────────────────────────────────────────────────────

    async def open_url(self, url: str) -> ActionResult:
        """Open a URL in the default browser."""
        t0 = time.monotonic()
        try:
            await self._run(webbrowser.open, url)
            await asyncio.sleep(1.5)   # give browser time to open
            return ActionResult(
                success=True, action="open_url",
                details={"url": url},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="open_url", error=str(e))

    async def open_app(self, app_name: str) -> ActionResult:
        """Open an application by name (Windows: start, Linux: xdg-open)."""
        t0 = time.monotonic()
        try:
            if sys.platform == "win32":
                await self._run(subprocess.Popen, ["start", app_name], shell=True)
            else:
                await self._run(subprocess.Popen, ["xdg-open", app_name])
            await asyncio.sleep(1.5)
            return ActionResult(
                success=True, action="open_app",
                details={"app": app_name},
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ActionResult(success=False, action="open_app", error=str(e))

    async def type_in_search_bar(self, text: str) -> ActionResult:
        """
        Open Windows search bar (Win key) and type text.
        Convenience wrapper for common task.
        """
        await self.press_key("win")
        await asyncio.sleep(0.5)
        await self.type_text(text)
        await asyncio.sleep(0.3)
        return ActionResult(success=True, action="type_in_search_bar",
                            details={"text": text})

    # ── Clipboard ─────────────────────────────────────────────────────────────

    async def copy(self) -> ActionResult:
        return await self.press_key("ctrl+c")

    async def paste(self) -> ActionResult:
        return await self.press_key("ctrl+v")

    async def select_all(self) -> ActionResult:
        return await self.press_key("ctrl+a")

    # ── Generic dispatch ──────────────────────────────────────────────────────

    async def execute(self, action_type: str, **params: Any) -> ActionResult:
        """Dispatch an action by name."""
        handlers = {
            "screenshot":         lambda: self.screenshot(),
            "click":              lambda: self.click(params["x"], params["y"],
                                                     params.get("button", "left"),
                                                     params.get("capture_after", False)),
            "double_click":       lambda: self.double_click(params["x"], params["y"]),
            "right_click":        lambda: self.right_click(params["x"], params["y"]),
            "move_to":            lambda: self.move_to(params["x"], params["y"],
                                                       params.get("duration", 0.3)),
            "drag":               lambda: self.drag(params["x1"], params["y1"],
                                                    params["x2"], params["y2"],
                                                    params.get("duration", 0.5)),
            "scroll":             lambda: self.scroll(params.get("x", 960), params.get("y", 540),
                                                      params.get("clicks", 3),
                                                      params.get("direction", "down")),
            "type_text":          lambda: self.type_text(params["text"],
                                                          params.get("interval", 0.03)),
            "press_key":          lambda: self.press_key(params["keys"]),
            "open_url":           lambda: self.open_url(params["url"]),
            "open_app":           lambda: self.open_app(params["app"]),
            "copy":               lambda: self.copy(),
            "paste":              lambda: self.paste(),
            "select_all":         lambda: self.select_all(),
            "type_in_search_bar": lambda: self.type_in_search_bar(params["text"]),
        }
        handler = handlers.get(action_type)
        if handler is None:
            return ActionResult(success=False, action=action_type,
                                error=f"Unknown action: {action_type}")
        try:
            return await handler()
        except Exception as e:
            return ActionResult(success=False, action=action_type, error=str(e))

    @property
    def is_available(self) -> bool:
        return self._available


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

_instance: ComputerAgent | None = None


def get_computer_agent() -> ComputerAgent:
    global _instance
    if _instance is None:
        _instance = ComputerAgent()
    return _instance
