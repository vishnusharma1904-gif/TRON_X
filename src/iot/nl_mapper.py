"""
TRON-X Natural Language → IoT Command Mapper
─────────────────────────────────────────────
Converts free-text user commands into structured HA service calls.

Two-stage approach:
  1. Regex/keyword fast-path for common patterns
  2. LLM fallback for complex/ambiguous commands
"""
from __future__ import annotations

import re
from typing import Optional

from src.core.logger import log


# ── Intent patterns ────────────────────────────────────────────────────────────

_ON_WORDS  = r"\b(turn on|switch on|enable|activate|open|start)\b"
_OFF_WORDS = r"\b(turn off|switch off|disable|deactivate|close|stop|shut)\b"
_DIM_WORDS = r"\b(dim|set brightness|brightness to|lower the light)\b"
_TEMP_WORDS = r"\b(set temperature|set thermostat|set temp|heat to|cool to)\b"
_COLOR_WORDS = r"\b(color|colour|set light to|change light to)\b"
_SCENE_WORDS = r"\b(scene|activate scene|set scene|run scene)\b"
_STATUS_WORDS = r"\b(status|what is|what'?s|is .+ on|state of|how is)\b"

# Common device name aliases → entity_id fragments
_DEVICE_ALIASES: dict[str, str] = {
    "living room light":    "light.living_room",
    "bedroom light":        "light.bedroom",
    "kitchen light":        "light.kitchen",
    "bathroom light":       "light.bathroom",
    "office light":         "light.office",
    "front door":           "lock.front_door",
    "back door":            "lock.back_door",
    "tv":                   "media_player.tv",
    "thermostat":           "climate.thermostat",
    "heater":               "climate.heater",
    "fan":                  "switch.fan",
    "ac":                   "climate.air_conditioner",
    "air conditioner":      "climate.air_conditioner",
    "coffee maker":         "switch.coffee_maker",
    "washing machine":      "switch.washing_machine",
    "garage":               "cover.garage",
    "curtains":             "cover.curtains",
    "blinds":               "cover.blinds",
}

_COLOR_MAP: dict[str, list[int]] = {
    "red":    [255, 0,   0],
    "green":  [0,   255, 0],
    "blue":   [0,   0,   255],
    "white":  [255, 255, 255],
    "warm":   [255, 200, 100],
    "cool":   [180, 210, 255],
    "yellow": [255, 255, 0],
    "purple": [150, 0,   255],
    "pink":   [255, 100, 150],
    "orange": [255, 128, 0],
    "cyan":   [0,   255, 255],
}


def _find_device(text: str) -> Optional[str]:
    """Find the best matching device entity_id from text."""
    text_lower = text.lower()
    # Longest-match first
    for alias in sorted(_DEVICE_ALIASES, key=len, reverse=True):
        if alias in text_lower:
            return _DEVICE_ALIASES[alias]
    return None


def _extract_number(text: str) -> Optional[float]:
    """Extract first numeric value from text."""
    m = re.search(r"\b(\d+(?:\.\d+)?)\b", text)
    return float(m.group(1)) if m else None


def _extract_color(text: str) -> Optional[list[int]]:
    text_lower = text.lower()
    for color, rgb in _COLOR_MAP.items():
        if color in text_lower:
            return rgb
    return None


def parse_command(text: str) -> Optional[dict]:
    """
    Fast-path NL → HA command.
    Returns dict with: domain, service, entity_id, extra (optional)
    Returns None if pattern doesn't match (caller should use LLM).
    """
    low = text.lower()
    device = _find_device(low)

    # Turn on
    if re.search(_ON_WORDS, low):
        if device:
            domain = device.split(".")[0]
            return {"domain": domain, "service": "turn_on", "entity_id": device}

    # Turn off
    if re.search(_OFF_WORDS, low):
        if device:
            domain = device.split(".")[0]
            return {"domain": domain, "service": "turn_off", "entity_id": device}

    # Dim / brightness
    if re.search(_DIM_WORDS, low):
        pct = _extract_number(low)
        if device and pct is not None:
            return {
                "domain": "light", "service": "turn_on", "entity_id": device,
                "extra": {"brightness_pct": max(0, min(100, int(pct)))},
            }

    # Temperature
    if re.search(_TEMP_WORDS, low):
        temp = _extract_number(low)
        if temp is not None:
            entity = device or "climate.thermostat"
            return {
                "domain": "climate", "service": "set_temperature",
                "entity_id": entity, "extra": {"temperature": temp},
            }

    # Color
    if re.search(_COLOR_WORDS, low):
        rgb = _extract_color(low)
        if device and rgb:
            return {
                "domain": "light", "service": "turn_on", "entity_id": device,
                "extra": {"rgb_color": rgb},
            }

    # Scene
    if re.search(_SCENE_WORDS, low):
        m = re.search(r"scene[:\s]+(\w+)", low)
        if m:
            return {"domain": "scene", "service": "turn_on",
                    "entity_id": f"scene.{m.group(1)}"}

    return None  # no fast-path match → caller should use LLM


async def nl_to_ha_command(
    text: str,
    ha_summary: str = "",
    persona: str = "jarvis",
) -> dict:
    """
    Full pipeline: fast-path, then semantic cache, then LLM fallback.
    Returns parsed HA command dict or error.
    """
    # 1. Fast-path
    cmd = parse_command(text)
    if cmd:
        log.info(f"[nl_mapper] Fast-path: {cmd}")
        return {**cmd, "method": "keyword", "confidence": 0.95}

    # 1b. Phase 22: semantic intent cache — a previously-resolved paraphrase
    #     of the same device action, served without an LLM round-trip.
    #     IntentCache.lookup() re-validates entity_id against
    #     _DEVICE_ALIASES before returning a hit, so a stale/renamed device
    #     never gets dispatched from cache (see intent_cache.py).
    try:
        from src.intelligence.intent_cache import get_intent_cache
        cached = await get_intent_cache().lookup(text)
        if cached and cached.intent == "iot" and cached.resolved_action.get("domain"):
            log.info(f"[nl_mapper] Cache hit (sim={cached.similarity:.3f}): {cached.resolved_action}")
            return {**cached.resolved_action, "method": "cache", "confidence": cached.similarity}
    except Exception as e:
        log.debug(f"[nl_mapper] intent cache lookup skipped: {e}")

    # 2. LLM fallback
    try:
        from src.intelligence.orchestrator import get_orchestrator
        orch = get_orchestrator()

        system_ctx = ha_summary[:2000] if ha_summary else ""
        prompt = (
            f"You control a smart home via Home Assistant.\n"
            f"{system_ctx}\n\n"
            f"User command: \"{text}\"\n\n"
            "Respond with ONLY a JSON object (no markdown) with these fields:\n"
            '  {"domain": "light", "service": "turn_on", '
            '"entity_id": "light.bedroom", "extra": {}}\n'
            "If you cannot map this to a HA command, respond: "
            '{"domain": null, "service": null, "entity_id": null, "extra": {}}'
        )
        result = await orch.chat(
            user_message=prompt,
            session_id="__iot_mapper__",
            intent="iot",
            persona=persona,
            max_tokens=120,
            temperature=0.1,
        )
        reply = result.get("reply", "").strip()

        # Parse JSON from reply
        import json as _json
        # Strip code fences if present
        clean = re.sub(r"```(?:json)?", "", reply).strip()
        parsed = _json.loads(clean)
        parsed["method"] = "llm"
        parsed["confidence"] = 0.7
        log.info(f"[nl_mapper] LLM: {parsed}")

        # Phase 22: cache a successful, entity-valid resolution so the next
        # paraphrase of this command skips the LLM entirely.
        if parsed.get("domain") and parsed.get("entity_id") in _DEVICE_ALIASES.values():
            try:
                from src.intelligence.intent_cache import get_intent_cache
                await get_intent_cache().store(text, "iot", parsed)
            except Exception as e:
                log.debug(f"[nl_mapper] intent cache store skipped: {e}")

        return parsed

    except Exception as e:
        log.warning(f"[nl_mapper] LLM fallback failed: {e}")
        return {"domain": None, "service": None, "entity_id": None,
                "extra": {}, "method": "failed", "error": str(e)}
