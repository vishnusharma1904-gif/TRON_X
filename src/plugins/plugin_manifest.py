"""
TRON-X Plugin Manifest Schema

A plugin is a directory containing:
    manifest.json   -- plugin metadata (this schema)
    __init__.py     -- entry point (must export an Agent-compatible class)

manifest.json example:
{
    "name": "weather_plugin",
    "version": "1.0.0",
    "description": "Fetches live weather and answers natural-language queries",
    "entry_module": "weather_plugin",
    "agent_class": "WeatherAgent",
    "capabilities": ["weather", "forecast", "climate"],
    "intent_keywords": ["weather", "temperature", "rain", "forecast"],
    "config_schema": {
        "api_key": {"type": "string", "required": false, "description": "OWM key"}
    },
    "requires": ["requests"],
    "author": "user",
    "enabled": true
}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class ConfigField(BaseModel):
    type:        str            = "string"
    required:    bool           = False
    default:     Any            = None
    description: str            = ""
    choices:     list[str]      = Field(default_factory=list)


class PluginManifest(BaseModel):
    """Validated plugin manifest loaded from manifest.json."""

    # Identity
    name:        str  = Field(..., min_length=1, max_length=64,
                              pattern=r"^[a-z0-9_\-]+$")
    version:     str  = Field(default="1.0.0")
    description: str  = Field(default="")
    author:      str  = Field(default="user")

    # Loading
    entry_module: str  = Field(..., description="Python module name inside plugin dir")
    agent_class:  str  = Field(default="Agent",
                               description="Class name to instantiate as the agent")

    # Capabilities / routing
    capabilities:    list[str] = Field(default_factory=list,
                                       description="Semantic tags for this plugin")
    intent_keywords: list[str] = Field(default_factory=list,
                                       description="Keywords that route to this plugin")

    # Configuration schema
    config_schema: dict[str, ConfigField] = Field(
        default_factory=dict,
        description="Declared config keys (not the values)",
    )

    # Dependencies
    requires: list[str] = Field(default_factory=list,
                                description="pip packages this plugin needs")

    # State
    enabled: bool = Field(default=True)

    @field_validator("name")
    @classmethod
    def _lower_name(cls, v: str) -> str:
        return v.lower().replace("-", "_")

    def dict_summary(self) -> dict:
        return {
            "name":         self.name,
            "version":      self.version,
            "description":  self.description,
            "author":       self.author,
            "capabilities": self.capabilities,
            "enabled":      self.enabled,
        }
