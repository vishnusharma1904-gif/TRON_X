"""
TRON-X Home Assistant Client  --  Phase 16 (extended)

Full REST integration with Home Assistant.
Phase 16 additions:
  - Scenes   : list, activate, apply (set multiple entities at once)
  - Scripts  : list, run
  - Automations: list, trigger, enable, disable
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from src.core.config import settings
from src.core.logger import log


class HomeAssistantClient:
    """
    REST client for Home Assistant.
    Base URL: settings.ha_url  (e.g. http://homeassistant.local:8123)
    Token:    settings.ha_token (Long-Lived Access Token)
    """

    def __init__(self):
        self._base  = (settings.ha_url   or "").rstrip("/")
        self._token =  settings.ha_token or ""
        self._headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type":  "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def enabled(self) -> bool:
        return bool(self._base and self._token)

    async def _http(self) -> httpx.AsyncClient:
        if not self._client or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers=self._headers,
                timeout=10,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    async def ping(self) -> bool:
        if not self.enabled:
            return False
        try:
            c = await self._http()
            r = await c.get("/api/")
            return r.status_code == 200
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # States
    # -----------------------------------------------------------------------

    async def get_states(self) -> list[dict]:
        if not self.enabled:
            return []
        try:
            c = await self._http()
            r = await c.get("/api/states")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("[ha] get_states failed: %s", e)
            return []

    async def get_state(self, entity_id: str) -> Optional[dict]:
        if not self.enabled:
            return None
        try:
            c = await self._http()
            r = await c.get(f"/api/states/{entity_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("[ha] get_state(%s) failed: %s", entity_id, e)
            return None

    async def get_entities_by_domain(self, domain: str) -> list[dict]:
        all_states = await self.get_states()
        return [s for s in all_states if s["entity_id"].startswith(f"{domain}.")]

    # -----------------------------------------------------------------------
    # Service calls
    # -----------------------------------------------------------------------

    async def call_service(
        self,
        domain:    str,
        service:   str,
        entity_id: Optional[str] = None,
        extra:     Optional[dict] = None,
    ) -> dict:
        if not self.enabled:
            return {"success": False, "error": "Home Assistant not configured"}

        payload: dict[str, Any] = {}
        if entity_id:
            payload["entity_id"] = entity_id
        if extra:
            payload.update(extra)

        try:
            c = await self._http()
            r = await c.post(f"/api/services/{domain}/{service}", json=payload)
            r.raise_for_status()
            log.info("[ha] %s.%s -> %s", domain, service, entity_id)
            return {
                "success":   True,
                "domain":    domain,
                "service":   service,
                "entity_id": entity_id,
                "response":  r.json(),
            }
        except Exception as e:
            log.warning("[ha] call_service failed: %s", e)
            return {"success": False, "error": str(e)}

    # -----------------------------------------------------------------------
    # Convenience wrappers
    # -----------------------------------------------------------------------

    async def turn_on(self, entity_id: str, **kwargs) -> dict:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_on", entity_id, kwargs or None)

    async def turn_off(self, entity_id: str) -> dict:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_off", entity_id)

    async def toggle(self, entity_id: str) -> dict:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "toggle", entity_id)

    async def set_light_color(
        self, entity_id: str, r: int, g: int, b: int, brightness: int = 255
    ) -> dict:
        return await self.call_service("light", "turn_on", entity_id, {
            "rgb_color":  [r, g, b],
            "brightness": brightness,
        })

    async def set_thermostat(self, entity_id: str, temperature: float) -> dict:
        return await self.call_service(
            "climate", "set_temperature", entity_id, {"temperature": temperature}
        )

    # -----------------------------------------------------------------------
    # Scenes  (Phase 16)
    # -----------------------------------------------------------------------

    async def get_scenes(self) -> list[dict]:
        """List all scenes registered in Home Assistant."""
        return await self.get_entities_by_domain("scene")

    async def activate_scene(self, scene_id: str) -> dict:
        """
        Activate a scene by ID.
        scene_id can be the full entity_id (scene.movie_night) or just the
        suffix (movie_night).
        """
        if not scene_id.startswith("scene."):
            scene_id = f"scene.{scene_id}"
        return await self.call_service("scene", "turn_on", scene_id)

    async def apply_scene(self, entities: dict[str, dict]) -> dict:
        """
        Apply an ad-hoc scene: set multiple entities to specific states
        without saving it as a named scene in HA.

        entities: { "light.bedroom": {"state": "on", "brightness": 128}, ... }
        """
        if not self.enabled:
            return {"success": False, "error": "Home Assistant not configured"}
        try:
            c = await self._http()
            r = await c.post("/api/services/scene/apply", json={"entities": entities})
            r.raise_for_status()
            return {"success": True, "entities_set": len(entities), "response": r.json()}
        except Exception as e:
            log.warning("[ha] apply_scene failed: %s", e)
            return {"success": False, "error": str(e)}

    async def create_scene(
        self,
        scene_id:  str,
        entities:  dict[str, dict],
        snapshot:  bool = False,
    ) -> dict:
        """
        Create or update a named scene in HA.

        scene_id: unique ID for the scene (without 'scene.' prefix)
        entities: { "light.bedroom": {"state": "on", "brightness": 200}, ... }
        snapshot: if True, HA captures current state of listed entities
        """
        if not self.enabled:
            return {"success": False, "error": "Home Assistant not configured"}
        try:
            payload: dict[str, Any] = {"scene_id": scene_id, "entities": entities}
            if snapshot:
                payload["snapshot_entities"] = list(entities.keys())
            c = await self._http()
            r = await c.post("/api/services/scene/create", json=payload)
            r.raise_for_status()
            return {"success": True, "scene_id": scene_id, "response": r.json()}
        except Exception as e:
            log.warning("[ha] create_scene failed: %s", e)
            return {"success": False, "error": str(e)}

    # -----------------------------------------------------------------------
    # Scripts  (Phase 16)
    # -----------------------------------------------------------------------

    async def get_scripts(self) -> list[dict]:
        """List all scripts registered in Home Assistant."""
        return await self.get_entities_by_domain("script")

    async def run_script(self, script_id: str, variables: Optional[dict] = None) -> dict:
        """
        Run a script by ID.
        script_id can be the full entity_id (script.good_morning) or suffix.
        variables: optional dict of script variables to pass.
        """
        if not script_id.startswith("script."):
            script_id = f"script.{script_id}"
        return await self.call_service("script", "turn_on", script_id, variables)

    # -----------------------------------------------------------------------
    # Automations  (Phase 16)
    # -----------------------------------------------------------------------

    async def get_automations(self) -> list[dict]:
        """List all automations registered in Home Assistant."""
        return await self.get_entities_by_domain("automation")

    async def trigger_automation(self, automation_id: str) -> dict:
        """Manually trigger an automation."""
        if not automation_id.startswith("automation."):
            automation_id = f"automation.{automation_id}"
        return await self.call_service("automation", "trigger", automation_id)

    async def enable_automation(self, automation_id: str) -> dict:
        """Enable (turn on) an automation."""
        if not automation_id.startswith("automation."):
            automation_id = f"automation.{automation_id}"
        return await self.call_service("automation", "turn_on", automation_id)

    async def disable_automation(self, automation_id: str) -> dict:
        """Disable (turn off) an automation."""
        if not automation_id.startswith("automation."):
            automation_id = f"automation.{automation_id}"
        return await self.call_service("automation", "turn_off", automation_id)

    async def reload_automations(self) -> dict:
        """Reload all automations from YAML (no entity_id needed)."""
        return await self.call_service("automation", "reload")

    # -----------------------------------------------------------------------
    # Sensor / light / switch helpers
    # -----------------------------------------------------------------------

    async def get_sensor_value(self, entity_id: str) -> Optional[str]:
        state = await self.get_state(entity_id)
        return state["state"] if state else None

    async def get_all_sensors(self) -> list[dict]:
        return await self.get_entities_by_domain("sensor")

    async def get_all_lights(self) -> list[dict]:
        return await self.get_entities_by_domain("light")

    async def get_all_switches(self) -> list[dict]:
        return await self.get_entities_by_domain("switch")

    # -----------------------------------------------------------------------
    # Template evaluation
    # -----------------------------------------------------------------------

    async def render_template(self, template: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            c = await self._http()
            r = await c.post("/api/template", json={"template": template})
            r.raise_for_status()
            return r.text
        except Exception as e:
            log.warning("[ha] template failed: %s", e)
            return None

    # -----------------------------------------------------------------------
    # LLM context summary
    # -----------------------------------------------------------------------

    async def home_summary(self, domains: list[str] | None = None) -> str:
        if not self.enabled:
            return "Home Assistant is not configured."

        domains = domains or ["light", "switch", "sensor", "climate",
                              "media_player", "scene", "automation"]
        all_states = await self.get_states()

        lines = ["=== Home State ==="]
        for domain in domains:
            entities = [
                s for s in all_states
                if s["entity_id"].startswith(f"{domain}.")
            ]
            if not entities:
                continue
            lines.append(f"\n[{domain.upper()}]")
            for e in entities[:20]:
                name  = e.get("attributes", {}).get("friendly_name", e["entity_id"])
                state = e["state"]
                unit  = e.get("attributes", {}).get("unit_of_measurement", "")
                lines.append(f"  {name}: {state}{' ' + unit if unit else ''}")

        return "\n".join(lines) if len(lines) > 1 else "No entities found."


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_ha_client: Optional[HomeAssistantClient] = None


def get_ha() -> HomeAssistantClient:
    global _ha_client
    if _ha_client is None:
        _ha_client = HomeAssistantClient()
    return _ha_client
