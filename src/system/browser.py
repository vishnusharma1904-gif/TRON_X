"""
TRON-X Browser Automation Agent
─────────────────────────────────
Playwright-based web browser agent.
Gracefully falls back with an error message if Playwright is not installed.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from src.core.logger import log


def _check_playwright() -> tuple[bool, str]:
    try:
        from playwright.async_api import async_playwright
        return True, ""
    except ImportError:
        return False, "pip install playwright && playwright install chromium"


async def open_url(url: str) -> dict:
    """Navigate to a URL and return page title + text excerpt."""
    ok, err = _check_playwright()
    if not ok:
        return {"success": False, "error": err}

    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=20000)
            title = await page.title()
            text  = await page.inner_text("body")
            await browser.close()

        excerpt = text[:2000].strip()
        log.info(f"[browser] Opened: {url} → '{title}'")
        return {"success": True, "url": url, "title": title, "text_excerpt": excerpt}

    except Exception as e:
        log.warning(f"[browser] open_url failed: {e}")
        return {"success": False, "url": url, "error": str(e)}


async def take_browser_screenshot(url: str, save_path: Optional[str] = None) -> dict:
    """Navigate to URL and take a screenshot."""
    ok, err = _check_playwright()
    if not ok:
        return {"success": False, "error": err}

    from playwright.async_api import async_playwright
    import time
    from pathlib import Path

    path = save_path or f"memory/cache/browser_{int(time.time())}.png"
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(url, timeout=20000)
            await page.screenshot(path=path, full_page=True)
            title = await page.title()
            await browser.close()

        log.info(f"[browser] Screenshot saved: {path}")
        return {"success": True, "url": url, "title": title, "screenshot": path}

    except Exception as e:
        return {"success": False, "url": url, "error": str(e)}


async def fill_form_and_submit(
    url: str,
    fields: dict[str, str],
    submit_selector: Optional[str] = None,
    confirm: bool = False,
) -> dict:
    """
    Fill a web form and optionally submit it.
    fields: {css_selector: value}
    Requires confirm=True to actually submit.
    """
    ok, err = _check_playwright()
    if not ok:
        return {"success": False, "error": err}

    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)  # visible for forms
            page = await browser.new_page()
            await page.goto(url, timeout=20000)

            for selector, value in fields.items():
                await page.fill(selector, value)

            if confirm and submit_selector:
                await page.click(submit_selector)
                await page.wait_for_load_state("networkidle", timeout=10000)
                result_url = page.url
                result_title = await page.title()
                await browser.close()
                return {"success": True, "submitted": True, "final_url": result_url, "final_title": result_title}
            else:
                await browser.close()
                return {"success": True, "submitted": False, "note": "Fields filled but not submitted (confirm=False)"}

    except Exception as e:
        return {"success": False, "error": str(e)}


async def search_web(query: str, engine: str = "google") -> dict:
    """
    Search the web using a browser (returns top results as text).
    Useful when API-based search isn't available.
    """
    ok, err = _check_playwright()
    if not ok:
        return {"success": False, "error": err}

    from playwright.async_api import async_playwright

    urls = {
        "google": f"https://www.google.com/search?q={query.replace(' ', '+')}",
        "bing":   f"https://www.bing.com/search?q={query.replace(' ', '+')}",
        "ddg":    f"https://duckduckgo.com/?q={query.replace(' ', '+')}",
    }
    url = urls.get(engine, urls["google"])

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=20000)
            # Extract visible text
            text = await page.inner_text("body")
            await browser.close()

        log.info(f"[browser] Web search: '{query}'")
        return {"success": True, "query": query, "engine": engine, "results": text[:3000]}

    except Exception as e:
        return {"success": False, "query": query, "error": str(e)}


async def click_element(url: str, selector: str, confirm: bool = False) -> dict:
    """Click an element on a web page. Requires confirm=True."""
    if not confirm:
        return {"success": False, "error": "Set confirm=True to perform click"}

    ok, err = _check_playwright()
    if not ok:
        return {"success": False, "error": err}

    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()
            await page.goto(url, timeout=20000)
            await page.click(selector, timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=10000)
            new_url   = page.url
            new_title = await page.title()
            await browser.close()

        log.info(f"[browser] Clicked '{selector}' on {url}")
        return {"success": True, "new_url": new_url, "new_title": new_title}

    except Exception as e:
        return {"success": False, "error": str(e)}
