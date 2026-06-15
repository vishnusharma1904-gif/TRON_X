"""
TRON-X Plugin Registry

Scans ~/.tronx/plugins/ for plugin directories, validates their manifest.json,
dynamically loads the entry module, and maintains a live registry of loaded agents.

Plugin directory layout:
    ~/.tronx/plugins/
        my_plugin/
            manifest.json
            __init__.py        <- must contain agent_class
            (any other files)

Usage:
    registry = get_registry()
    await registry.scan()                      # discover + load all enabled plugins
    agent_cls = registry.get_agent("my_plugin")
    result    = await registry.run("my_plugin", {"task": "..."})
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from src.core.logger import log
from src.plugins.plugin_manifest import PluginManifest


# Default plugin root; can be overridden by TRONX_PLUGIN_DIR env var
DEFAULT_PLUGIN_DIR = Path.home() / ".tronx" / "plugins"


class LoadedPlugin:
    """Runtime wrapper around a loaded plugin."""

    def __init__(
        self,
        manifest: PluginManifest,
        plugin_dir: Path,
        agent_instance: Any,
    ):
        self.manifest       = manifest
        self.plugin_dir     = plugin_dir
        self.agent          = agent_instance
        self.load_error:    Optional[str] = None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def enabled(self) -> bool:
        return self.manifest.enabled

    def to_dict(self) -> dict:
        return {
            **self.manifest.dict_summary(),
            "plugin_dir": str(self.plugin_dir),
            "load_error": self.load_error,
            "agent_type": type(self.agent).__name__ if self.agent else None,
        }


class PluginRegistry:
    """Singleton registry that loads and manages TRON-X plugins."""

    def __init__(self, plugin_dir: Optional[Path] = None):
        import os
        env_dir = os.environ.get("TRONX_PLUGIN_DIR")
        self._plugin_dir: Path = Path(env_dir) if env_dir else (plugin_dir or DEFAULT_PLUGIN_DIR)
        self._plugins:    dict[str, LoadedPlugin] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def plugin_dir(self) -> Path:
        return self._plugin_dir

    async def scan(self) -> list[str]:
        """Scan plugin_dir and load all valid, enabled plugins. Returns loaded names."""
        self._plugin_dir.mkdir(parents=True, exist_ok=True)
        loaded = []
        for path in sorted(self._plugin_dir.iterdir()):
            if not path.is_dir():
                continue
            manifest_file = path / "manifest.json"
            if not manifest_file.exists():
                continue
            name = await self._load_plugin(path)
            if name:
                loaded.append(name)
        log.info("[plugins] Scan complete — %d plugin(s) loaded", len(loaded))
        return loaded

    async def load(self, plugin_dir: Path) -> Optional[str]:
        """Load (or reload) a single plugin from its directory."""
        return await self._load_plugin(plugin_dir)

    async def reload(self, name: str) -> Optional[str]:
        """Reload a plugin by name (re-reads manifest, re-imports module)."""
        plugin = self._plugins.get(name)
        if not plugin:
            raise KeyError(f"Plugin '{name}' not found")
        return await self._load_plugin(plugin.plugin_dir)

    def get(self, name: str) -> Optional[LoadedPlugin]:
        return self._plugins.get(name)

    def list_all(self) -> list[dict]:
        return [p.to_dict() for p in self._plugins.values()]

    def list_enabled(self) -> list[dict]:
        return [p.to_dict() for p in self._plugins.values() if p.enabled]

    def get_agent(self, name: str) -> Any:
        """Return the instantiated agent object for a plugin."""
        plugin = self._plugins.get(name)
        if not plugin:
            raise KeyError(f"Plugin '{name}' not loaded")
        if not plugin.enabled:
            raise RuntimeError(f"Plugin '{name}' is disabled")
        if plugin.agent is None:
            raise RuntimeError(f"Plugin '{name}' failed to load agent")
        return plugin.agent

    async def run(self, name: str, payload: dict) -> dict:
        """Invoke a plugin's agent.run() method."""
        agent = self.get_agent(name)
        try:
            if hasattr(agent, "run"):
                result = await agent.run(**payload) if _is_coro(agent.run) else agent.run(**payload)
                return result if isinstance(result, dict) else {"result": result, "success": True}
            return {"error": f"Plugin '{name}' agent has no run() method", "success": False}
        except Exception as e:
            log.error("[plugins] Error running '%s': %s", name, e)
            return {"error": str(e), "success": False}

    def enable(self, name: str) -> None:
        plugin = self._plugins.get(name)
        if not plugin:
            raise KeyError(f"Plugin '{name}' not found")
        plugin.manifest.enabled = True
        self._save_manifest(plugin)

    def disable(self, name: str) -> None:
        plugin = self._plugins.get(name)
        if not plugin:
            raise KeyError(f"Plugin '{name}' not found")
        plugin.manifest.enabled = False
        self._save_manifest(plugin)

    def unload(self, name: str) -> bool:
        if name in self._plugins:
            del self._plugins[name]
            return True
        return False

    def capabilities_map(self) -> dict[str, list[str]]:
        """Returns {plugin_name: [capabilities]} for all enabled plugins."""
        return {
            p.name: p.manifest.capabilities
            for p in self._plugins.values()
            if p.enabled
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_plugin(self, plugin_dir: Path) -> Optional[str]:
        manifest_file = plugin_dir / "manifest.json"
        if not manifest_file.exists():
            log.warning("[plugins] No manifest.json in %s — skipped", plugin_dir)
            return None

        # Parse manifest
        try:
            raw = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest = PluginManifest(**raw)
        except Exception as e:
            log.error("[plugins] Invalid manifest in %s: %s", plugin_dir, e)
            return None

        if not manifest.enabled:
            log.debug("[plugins] '%s' is disabled — skipped", manifest.name)
            return None

        # Auto-install missing requirements
        if manifest.requires:
            await self._ensure_requirements(manifest.name, manifest.requires)

        # Dynamically import the entry module
        agent_instance = None
        load_error     = None
        try:
            module = _import_from_dir(plugin_dir, manifest.entry_module)
            agent_cls = getattr(module, manifest.agent_class, None)
            if agent_cls is None:
                raise AttributeError(
                    f"Class '{manifest.agent_class}' not found in module '{manifest.entry_module}'"
                )
            agent_instance = agent_cls()
            log.info("[plugins] Loaded '%s' v%s (%s)", manifest.name, manifest.version, manifest.agent_class)
        except Exception as e:
            load_error = str(e)
            log.error("[plugins] Failed to load '%s': %s", manifest.name, e)

        loaded = LoadedPlugin(
            manifest=manifest,
            plugin_dir=plugin_dir,
            agent_instance=agent_instance,
        )
        loaded.load_error = load_error
        self._plugins[manifest.name] = loaded
        return manifest.name if agent_instance else None

    async def _ensure_requirements(self, plugin_name: str, packages: list[str]) -> None:
        for pkg in packages:
            try:
                importlib.util.find_spec(pkg.replace("-", "_").split("[")[0])
            except (ModuleNotFoundError, ValueError):
                log.info("[plugins] '%s' needs '%s' — installing...", plugin_name, pkg)
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg,
                     "--break-system-packages", "--quiet"],
                    check=False,
                )

    def _save_manifest(self, plugin: LoadedPlugin) -> None:
        """Persist enabled/disabled state back to manifest.json."""
        try:
            manifest_file = plugin.plugin_dir / "manifest.json"
            data = plugin.manifest.model_dump(exclude_none=False)
            # config_schema has ConfigField objects; convert to dicts
            data["config_schema"] = {
                k: v.model_dump() for k, v in plugin.manifest.config_schema.items()
            }
            manifest_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.error("[plugins] Could not save manifest for '%s': %s", plugin.name, e)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _import_from_dir(plugin_dir: Path, module_name: str):
    """Import a module from a plugin directory by injecting the dir into sys.path."""
    plugin_dir_str = str(plugin_dir)
    injected = plugin_dir_str not in sys.path
    if injected:
        sys.path.insert(0, plugin_dir_str)
    try:
        # Force reimport in case of reload
        if module_name in sys.modules:
            del sys.modules[module_name]
        return importlib.import_module(module_name)
    finally:
        if injected and plugin_dir_str in sys.path:
            sys.path.remove(plugin_dir_str)


def _is_coro(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_registry: Optional[PluginRegistry] = None


def get_registry() -> PluginRegistry:
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry
