"""
TRON-X Plugin Management API

GET    /api/plugins                  -- list all plugins (summary)
GET    /api/plugins/capabilities     -- capability map for enabled plugins
GET    /api/plugins/{name}           -- plugin detail
POST   /api/plugins/scan             -- (re)scan plugin dir and load new plugins
POST   /api/plugins/{name}/reload    -- reload a specific plugin
POST   /api/plugins/{name}/enable    -- enable a disabled plugin
POST   /api/plugins/{name}/disable   -- disable a plugin (stays loaded, won't run)
DELETE /api/plugins/{name}           -- unload plugin from registry (does not delete files)
POST   /api/plugins/{name}/run       -- invoke a plugin's agent directly
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any

from src.plugins.plugin_registry import get_registry

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class RunPluginReq(BaseModel):
    payload: dict = Field(default_factory=dict,
                          description="Keyword args forwarded to plugin agent.run()")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_plugins(enabled_only: bool = False):
    """List all plugins known to the registry."""
    registry = get_registry()
    plugins = registry.list_enabled() if enabled_only else registry.list_all()
    return {
        "total":       len(plugins),
        "enabled":     sum(1 for p in plugins if p.get("enabled")),
        "plugin_dir":  str(registry.plugin_dir),
        "plugins":     plugins,
    }


@router.get("/capabilities")
async def plugin_capabilities():
    """Return capability tags for all enabled plugins."""
    return {
        "capabilities": get_registry().capabilities_map(),
    }


@router.get("/{name}")
async def get_plugin(name: str):
    """Get full detail for a single plugin."""
    plugin = get_registry().get(name)
    if not plugin:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    return plugin.to_dict()


@router.post("/scan")
async def scan_plugins():
    """Scan ~/.tronx/plugins/ and load any new valid plugins."""
    loaded = await get_registry().scan()
    # Re-register newly loaded plugins with TaskCoordinator
    _sync_coordinator()
    return {
        "loaded":     loaded,
        "total_loaded": len(loaded),
    }


@router.post("/{name}/reload")
async def reload_plugin(name: str):
    """Reload a plugin — re-reads manifest and re-imports module."""
    try:
        result = await get_registry().reload(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    _sync_coordinator()
    return {
        "status":  "reloaded" if result else "reload_failed",
        "plugin":  name,
    }


@router.post("/{name}/enable")
async def enable_plugin(name: str):
    """Enable a previously disabled plugin."""
    try:
        get_registry().enable(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    _sync_coordinator()
    return {"status": "enabled", "plugin": name}


@router.post("/{name}/disable")
async def disable_plugin(name: str):
    """Disable a plugin (it stays loaded in memory but cannot be invoked)."""
    try:
        get_registry().disable(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    _sync_coordinator()
    return {"status": "disabled", "plugin": name}


@router.delete("/{name}")
async def unload_plugin(name: str):
    """Unload a plugin from the registry (does not delete files on disk)."""
    removed = get_registry().unload(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not loaded")
    _sync_coordinator()
    return {"status": "unloaded", "plugin": name}


@router.post("/{name}/run")
async def run_plugin(name: str, req: RunPluginReq):
    """Directly invoke a plugin agent with the given payload."""
    registry = get_registry()
    plugin = registry.get(name)
    if not plugin:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    if not plugin.enabled:
        raise HTTPException(status_code=409, detail=f"Plugin '{name}' is disabled")
    result = await registry.run(name, req.payload)
    return result


# ---------------------------------------------------------------------------
# Coordinator sync helper
# ---------------------------------------------------------------------------

def _sync_coordinator() -> None:
    """Register / unregister plugin agents in TaskCoordinator so they are
    accessible via /api/agents/coordinate endpoints."""
    try:
        from src.agents.coordinator import TaskCoordinator
        registry = get_registry()

        for plugin in registry.list_enabled():
            name = plugin["name"]
            loaded = registry.get(name)
            if loaded and loaded.agent:
                # Wrap the plugin's run() as an async coordinator handler
                _register_plugin_agent(TaskCoordinator, name, loaded)
    except Exception as e:
        # Non-fatal — coordinator might not be loaded yet at startup
        from src.core.logger import log
        log.debug("[plugins] Coordinator sync skipped: %s", e)


def _register_plugin_agent(coordinator_cls, name: str, loaded) -> None:
    """Inject a thin async handler into the module-level _REGISTRY for this plugin."""
    import src.agents.coordinator as _coord_mod

    async def _plugin_handler(payload: dict) -> dict:
        return await get_registry().run(name, payload)

    # coordinator.py uses a module-level _REGISTRY dict with "fn" key
    _coord_mod._REGISTRY[name] = {
        "fn":          _plugin_handler,
        "description": loaded.manifest.description,
    }
