#!/usr/bin/env python3
"""
TRON-X — Ollama setup/diagnostic helper (Phase 29).

Usage:
    python3 scripts/check_ollama.py

1. Checks whether Ollama's API is reachable at settings.ollama_base_url.
2. If unreachable: prints install instructions (https://ollama.com/download)
   and how to enable the local fallback in .env.
3. If reachable: lists installed models, compares them against the `ollama`
   provider's model list in config/models.json (used by
   src/intelligence/router.py's _OLLAMA_INTENT_MAP / _OLLAMA_FALLBACK_PRIORITY),
   and prints `ollama pull <model>` for any that are missing.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import httpx  # noqa: E402

from src.core.config import get_settings  # noqa: E402

settings = get_settings()


def main() -> int:
    base_url = settings.ollama_base_url
    print(f"TRON-X Ollama check — base URL: {base_url}\n")

    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=3.0)
        resp.raise_for_status()
    except Exception as e:
        print(f"Ollama is NOT reachable at {base_url} ({type(e).__name__}: {e})\n")
        print("Install Ollama:  https://ollama.com/download")
        print("After installing, Ollama usually runs as a background service.")
        print("Then in .env, set:")
        print("  ollama_base_url=http://localhost:11434")
        print("  ollama_fallback_enabled=true   (default — local fallback when cloud is exhausted)")
        print("  ollama_enabled=true            (optional — also offer Ollama as a normal chain member)")
        return 1

    data = resp.json()
    installed = sorted(m.get("name", "") for m in data.get("models", []))
    print("Ollama is reachable. Installed models:")
    if installed:
        for name in installed:
            print(f"  - {name}")
    else:
        print("  (none)")

    with open("config/models.json") as f:
        catalog = json.load(f)
    wanted = catalog.get("provider_configs", {}).get("ollama", {}).get("_models", [])
    wanted_names = [m.split("/", 1)[1] for m in wanted]

    installed_bases = {n.split(":")[0] for n in installed}

    missing = [m for m in wanted_names if m.split(":")[0] not in installed_bases]
    print(f"\nconfig/models.json expects {len(wanted_names)} ollama model(s) "
          f"(used as fallback targets by the router).")
    if missing:
        print("Missing — pull these to enable full local fallback coverage:")
        for m in missing:
            print(f"  ollama pull {m}")
    else:
        print("All expected fallback models are installed.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
