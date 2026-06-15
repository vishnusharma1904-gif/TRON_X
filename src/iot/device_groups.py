"""
TRON-X Device Group Manager  --  Phase 16

Groups multiple Home Assistant entities under a single named alias.
Groups are persisted to ~/.tronx/device_groups.json and survive restarts.

Example group:
    {
      "name":        "living_room",
      "description": "All lights and fan in the living room",
      "entities":    ["light.sofa", "light.ceiling", "light.lamp", "switch.fan"],
      "created_at":  1720000000.0
    }

Control a whole group with one call -- all service calls run concurrently
via asyncio.gather, so a group of 10 devices takes the same time as 1.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Optional

from src.core.logger import log

_GROUPS_FILE = Path.home() / ".tronx" / "device_groups.json"


def _load() -> dict[str, dict]:
    try:
        _GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _GROUPS_FILE.exists():
            return json.loads(_GROUPS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("[groups] Load failed: %s", e)
    return {}


def _save(groups: dict[str, dict]) -> None:
    try:
        _GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GROUPS_FILE.write_text(
            json.dumps(groups, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        log.error("[groups] Save failed: %s", e)


# ---------------------------------------------------------------------------
# DeviceGroupManager
# ---------------------------------------------------------------------------

class DeviceGroupManager:
    """Manages named groups of Home Assistant entity IDs."""

    def __init__(self):
        self._groups: dict[str, dict] = _load()
        log.info("[groups] Loaded %d device groups", len(self._groups))

    # -----------------------------------------------------------------------
    # CRUD
    # -----------------------------------------------------------------------

    def create_group(
        self,
        name:        str,
        entities:    list[str],
        description: str = "",
    ) -> dict:
        """
        Create or overwrite a device group.

        Returns the created group dict.
        """
        name = name.lower().strip().replace(" ", "_")
        if not name:
            return {"error": "Group name cannot be empty"}
        if not entities:
            return {"error": "Group must contain at least one entity"}

        group = {
            "name":        name,
            "description": description.strip(),
            "entities":    [e.strip() for e in entities if e.strip()],
            "created_at":  time.time(),
        }
        self._groups[name] = group
        _save(self._groups)
        log.info("[groups] Created group '%s' with %d entities", name, len(entities))
        return group

    def update_group(
        self,
        name:        str,
        entities:    Optional[list[str]] = None,
        description: Optional[str]       = None,
    ) -> dict:
        """Update entities or description of an existing group."""
        name = name.lower().strip().replace(" ", "_")
        if name not in self._groups:
            return {"error": f"Group '{name}' not found"}

        if entities is not None:
            self._groups[name]["entities"] = [e.strip() for e in entities if e.strip()]
        if description is not None:
            self._groups[name]["description"] = description.strip()
        self._groups[name]["updated_at"] = time.time()

        _save(self._groups)
        return self._groups[name]

    def delete_group(self, name: str) -> dict:
        name = name.lower().strip().replace(" ", "_")
        if name not in self._groups:
            return {"error": f"Group '{name}' not found"}
        deleted = self._groups.pop(name)
        _save(self._groups)
        log.info("[groups] Deleted group '%s'", name)
        return {"deleted": True, "group": deleted}

    def get_group(self, name: str) -> Optional[dict]:
        return self._groups.get(name.lower().strip().replace(" ", "_"))

    def list_groups(self) -> list[dict]:
        return list(self._groups.values())

    # -----------------------------------------------------------------------
    # Control
    # -----------------------------------------------------------------------

    async def control_group(
        self,
        name:      str,
        service:   str,           # e.g. "turn_on", "turn_off", "toggle"
        domain:    Optional[str] = None,   # if None, derived per entity
        extra:     Optional[dict] = None,
    ) -> dict:
        """
        Call a HA service on every entity in the group concurrently.

        Returns:
            {
              "group":   str,
              "service": str,
              "results": [{"entity_id": str, "success": bool, ...}],
              "success_count": int,
              "fail_count":    int,
            }
        """
        from src.iot.home_assistant import get_ha

        name = name.lower().strip().replace(" ", "_")
        group = self._groups.get(name)
        if not group:
            return {"error": f"Group '{name}' not found"}

        ha = get_ha()
        entities = group["entities"]

        async def _call_one(entity_id: str) -> dict:
            d = domain or entity_id.split(".")[0]
            result = await ha.call_service(d, service, entity_id, extra)
            result["entity_id"] = entity_id
            return result

        results = await asyncio.gather(*[_call_one(e) for e in entities])

        success = sum(1 for r in results if r.get("success"))
        return {
            "group":         name,
            "service":       service,
            "entity_count":  len(entities),
            "success_count": success,
            "fail_count":    len(entities) - success,
            "results":       list(results),
        }

    async def group_on(self, name: str, extra: Optional[dict] = None) -> dict:
        """Turn on all entities in a group."""
        return await self.control_group(name, "turn_on", extra=extra)

    async def group_off(self, name: str) -> dict:
        """Turn off all entities in a group."""
        return await self.control_group(name, "turn_off")

    async def group_toggle(self, name: str) -> dict:
        """Toggle all entities in a group."""
        return await self.control_group(name, "toggle")

    async def group_status(self, name: str) -> dict:
        """
        Return the current HA state for every entity in the group.

        Returns:
            {
              "group":    str,
              "entities": [{"entity_id": str, "state": str, "attributes": dict}],
              "on_count": int, "off_count": int, "unknown_count": int,
            }
        """
        from src.iot.home_assistant import get_ha

        name = name.lower().strip().replace(" ", "_")
        group = self._groups.get(name)
        if not group:
            return {"error": f"Group '{name}' not found"}

        ha      = get_ha()
        states  = await asyncio.gather(*[ha.get_state(e) for e in group["entities"]])
        results = []
        on_c = off_c = unk_c = 0
        for entity_id, state in zip(group["entities"], states):
            if state:
                s = state["state"]
                results.append({
                    "entity_id":  entity_id,
                    "state":      s,
                    "attributes": state.get("attributes", {}),
                })
                if s == "on":
                    on_c += 1
                elif s == "off":
                    off_c += 1
                else:
                    unk_c += 1
            else:
                results.append({"entity_id": entity_id, "state": "unavailable", "attributes": {}})
                unk_c += 1

        return {
            "group":         name,
            "description":   group.get("description", ""),
            "entities":      results,
            "on_count":      on_c,
            "off_count":     off_c,
            "unknown_count": unk_c,
        }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_manager: Optional[DeviceGroupManager] = None


def get_device_groups() -> DeviceGroupManager:
    global _manager
    if _manager is None:
        _manager = DeviceGroupManager()
    return _manager
