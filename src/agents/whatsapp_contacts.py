"""
TRON-X WhatsApp Contacts
─────────────────────────
A tiny JSON name→number book so you can say "message mom" instead of typing a
number. Thread-safe, atomic writes. Numbers are stored as digits-only E.164.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from typing import Optional

from src.core.config import settings
from src.core.logger import log

_lock = threading.RLock()


def _path() -> str:
    return settings.whatsapp_contacts_path


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def load() -> dict[str, str]:
    """Return {name_lower: digits}. Missing/corrupt file -> {}."""
    p = _path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k).strip().lower(): _digits(str(v)) for k, v in data.items() if _digits(str(v))}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[contacts] could not load ({p}): {e}")
    return {}


def _save(d: dict[str, str]) -> None:
    p = _path()
    directory = os.path.dirname(p) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        log.error(f"[contacts] could not save: {e}")


def add(name: str, number: str) -> dict:
    """Add/replace a contact. Returns {name, number} or {error}."""
    name = (name or "").strip()
    digits = _digits(number)
    if not name:
        return {"error": "contact name is empty"}
    if not (7 <= len(digits) <= 15):
        return {"error": f"'{number}' is not a valid phone number"}
    with _lock:
        d = load()
        d[name.lower()] = digits
        _save(d)
    return {"name": name, "number": digits}


def resolve(name: str) -> Optional[str]:
    """Return the number for a contact name (exact, else unique substring)."""
    if not name:
        return None
    key = name.strip().lower()
    d = load()
    if key in d:
        return d[key]
    hits = {num for nm, num in d.items() if key in nm or nm in key}
    if len(hits) == 1:
        return next(iter(hits))
    return None
