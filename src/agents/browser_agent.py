"""
BrowserAgent — Playwright-based browser automation for TRON-X.

On Windows, Playwright needs a ProactorEventLoop to spawn the Chromium
subprocess.  Uvicorn (especially with --reload) runs a SelectorEventLoop,
so we can't launch Playwright directly in uvicorn's loop.

Fix: a private *dedicated* ProactorEventLoop runs in a background daemon
thread.  All Playwright work runs there.  Public async methods submit
coroutines to that thread-loop via asyncio.run_coroutine_threadsafe() and
await the result through run_in_executor(), so uvicorn's loop stays free.
"""
from __future__ import annotations

import asyncio
import base64
import sys
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Optional

from src.core.logger import log

# ---------------------------------------------------------------------------
# Dedicated Playwright event loop (daemon thread)
# ---------------------------------------------------------------------------

_pw_loop: asyncio.AbstractEventLoop | None = None
_pw_loop_thread: threading.Thread | None = None
_pw_loop_lock = threading.Lock()


def _get_playwright_loop() -> asyncio.AbstractEventLoop:
    """Return (creating if needed) a running ProactorEventLoop on its own thread."""
    global _pw_loop, _pw_loop_thread
    with _pw_loop_lock:
        if _pw_loop is None or _pw_loop.is_closed():
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()

            def _run_forever():
                asyncio.set_event_loop(loop)
                loop.run_forever()

            t = threading.Thread(target=_run_forever, daemon=True, name="playwright-loop")
            t.start()
            _pw_loop = loop
            _pw_loop_thread = t
            log.debug("[BrowserAgent] Playwright loop started on dedicated thread")
    return _pw_loop


# ---------------------------------------------------------------------------
# Instance lock (lives in uvicorn's loop)
# ---------------------------------------------------------------------------

_instance_lock: asyncio.Lock | None = None


def _get_instance_lock() -> asyncio.Lock:
    global _instance_lock
    if _instance_lock is None:
        _instance_lock = asyncio.Lock()
    return _instance_lock


# ---------------------------------------------------------------------------
# BrowserAgent
# ---------------------------------------------------------------------------

class BrowserAgent:
    _instance: BrowserAgent | None = None

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._context = None
        self._started: bool = False
        self._playwright_error: str | None = None
        self._check_playwright()

    # ------------------------------------------------------------------
    # Playwright availability check
    # ------------------------------------------------------------------

    def _check_playwright(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._async_playwright = async_playwright
        except ImportError:
            self._playwright_error = (
                "Playwright is not installed. "
                "Install with: pip install playwright && playwright install chromium"
            )

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    async def get(cls) -> BrowserAgent:
        lock = _get_instance_lock()
        async with lock:
            if cls._instance is None or not cls._instance._started:
                cls._instance = cls()
                await cls._instance.start()
            return cls._instance

    # ------------------------------------------------------------------
    # Bridge: submit a coroutine to the Playwright loop and await result
    # ------------------------------------------------------------------

    async def _pw(self, coro, timeout: float = 30.0):
        """
        Run `coro` in the dedicated Playwright event loop and return its result.
        Uses run_in_executor so uvicorn's loop is never blocked.
        """
        loop = _get_playwright_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, future.result, timeout
            )
        except FutureTimeout:
            future.cancel()
            raise asyncio.TimeoutError(f"Browser operation timed out after {timeout}s")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._playwright_error:
            log.error(f"[BrowserAgent] Cannot start — {self._playwright_error}")
            return

        async def _do_start():
            pw = await self._async_playwright().start()
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
            )
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            return pw, browser, context

        try:
            result = await self._pw(_do_start(), timeout=30.0)
            self._pw_handle, self._browser_handle, self._context_handle = result
            self._started = True
            log.info("[BrowserAgent] Started — persistent Chromium context ready")
        except Exception as e:
            log.error(f"[BrowserAgent] start() failed: {e}")
            self._started = False

    async def stop(self) -> None:
        if not self._started:
            return

        async def _do_stop():
            try:
                await self._context_handle.close()
            except Exception:
                pass
            try:
                await self._browser_handle.close()
            except Exception:
                pass
            try:
                await self._pw_handle.stop()
            except Exception:
                pass

        try:
            await self._pw(_do_stop(), timeout=10.0)
        except Exception as e:
            log.warning(f"[BrowserAgent] stop() error: {e}")
        finally:
            self._started = False
            BrowserAgent._instance = None
            log.info("[BrowserAgent] Stopped")

    def _guard(self) -> dict | None:
        if not self._started:
            msg = self._playwright_error or "BrowserAgent not started"
            return {"success": False, "error": msg}
        return None

    # ------------------------------------------------------------------
    # Actions (each runs its Playwright work inside the PW loop)
    # ------------------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                resp = await page.goto(url, timeout=20_000, wait_until=wait_until)
                return {
                    "success": True,
                    "url": page.url,
                    "title": await page.title(),
                    "status": resp.status if resp else None,
                }
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] navigate({url}): {e}")
            return {"success": False, "error": str(e)}

    async def get_text(self, url: str, selector: str = "body") -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                text = await page.inner_text(selector)
                return {"success": True, "url": url, "selector": selector, "text": text[:4000]}
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] get_text({url}): {e}")
            return {"success": False, "error": str(e)}

    async def click(self, url: str, selector: str) -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                await page.click(selector, timeout=5_000)
                await page.wait_for_load_state("networkidle", timeout=8_000)
                return {
                    "success": True,
                    "clicked": selector,
                    "final_url": page.url,
                    "final_title": await page.title(),
                }
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] click({url}, {selector}): {e}")
            return {"success": False, "error": str(e)}

    async def fill(self, url: str, fields: dict[str, str], submit_selector: str | None = None) -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                for sel, value in fields.items():
                    await page.locator(sel).fill(value)
                submitted = False
                if submit_selector:
                    await page.locator(submit_selector).click(timeout=5_000)
                    await page.wait_for_load_state("networkidle", timeout=8_000)
                    submitted = True
                return {
                    "success": True,
                    "filled": list(fields.keys()),
                    "submitted": submitted,
                    "final_url": page.url,
                }
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] fill({url}): {e}")
            return {"success": False, "error": str(e)}

    async def scroll(self, url: str, direction: str = "down", amount: int = 500) -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                delta = amount if direction == "down" else -amount
                await page.evaluate(f"window.scrollBy(0, {delta})")
                return {"success": True, "direction": direction, "amount": amount}
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] scroll({url}): {e}")
            return {"success": False, "error": str(e)}

    async def screenshot(self, url: str, save_path: str | None = None, return_base64: bool = False) -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                path_str = save_path or str(Path(f"memory/cache/browser_{int(time.time())}.png"))
                Path(path_str).parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=path_str, full_page=True)
                b64_str: str | None = None
                if return_base64:
                    b64_str = base64.b64encode(Path(path_str).read_bytes()).decode("utf-8")
                return {"success": True, "url": url, "path": path_str, "base64": b64_str}
            finally:
                await page.close()

        try:
            return await self._pw(_do(), timeout=40.0)
        except Exception as e:
            log.error(f"[BrowserAgent] screenshot({url}): {e}")
            return {"success": False, "error": str(e)}

    async def scrape(self, url: str) -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                title = await page.title()
                final_url = page.url
                text = (await page.inner_text("body"))[:5000]
                links_js = await page.evaluate("""
                    () => {
                        const anchors = document.querySelectorAll('a[href]');
                        return Array.from(anchors).slice(0, 20).map(e => ({
                            text: (e.innerText || '').trim(),
                            href: e.href
                        }));
                    }
                """)
                return {"success": True, "url": final_url, "title": title, "text": text, "links": links_js}
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] scrape({url}): {e}")
            return {"success": False, "error": str(e)}

    async def search_google(self, query: str) -> dict:
        err = self._guard()
        if err:
            return err

        async def _do():
            page = await self._context_handle.new_page()
            try:
                search_url = "https://www.google.com/search?q=" + query.replace(" ", "+")
                await page.goto(search_url, timeout=20_000, wait_until="domcontentloaded")
                snippets = await page.evaluate("""
                    () => {
                        const cards = document.querySelectorAll('div.g');
                        return Array.from(cards).slice(0, 5).map(e => ({
                            title: e.querySelector('h3')?.innerText || null,
                            url: e.querySelector('a')?.href || null,
                            snippet: e.querySelector('.VwiC3b')?.innerText || null
                        })).filter(r => r.title || r.snippet);
                    }
                """)
                return {"success": True, "query": query, "results": snippets}
            finally:
                await page.close()

        try:
            return await self._pw(_do())
        except Exception as e:
            log.error(f"[BrowserAgent] search_google({query}): {e}")
            return {"success": False, "error": str(e)}

    async def action(self, action_type: str, **kwargs) -> dict:
        err = self._guard()
        if err:
            return err
        mapping = {
            "navigate":      self.navigate,
            "get_text":      self.get_text,
            "click":         self.click,
            "fill":          self.fill,
            "scroll":        self.scroll,
            "screenshot":    self.screenshot,
            "scrape":        self.scrape,
            "search_google": self.search_google,
        }
        handler = mapping.get(action_type)
        if handler is None:
            return {"success": False, "error": f"Unknown action: {action_type}"}
        try:
            return await handler(**kwargs)
        except Exception as e:
            log.error(f"[BrowserAgent] action({action_type}): {e}")
            return {"success": False, "error": str(e)}
