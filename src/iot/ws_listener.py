"""
TRON-X Home Assistant WebSocket Listener
──────────────────────────────────────────
Real-time event subscription via HA WebSocket API.
Runs in a background asyncio task; callbacks fired on state_changed events.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Optional

from src.core.config import settings
from src.core.logger import log


class HAWebSocketListener:
    """
    Connects to HA WS API and subscribes to state_changed events.
    Callbacks receive (entity_id, old_state, new_state).
    """

    def __init__(self):
        self._ws_url = (
            (settings.ha_url or "http://localhost:8123")
            .replace("http://", "ws://")
            .replace("https://", "wss://")
            .rstrip("/") + "/api/websocket"
        )
        self._token   = settings.ha_token or ""
        self._ws      = None
        self._msg_id  = 1
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list[Callable] = []
        self._entity_filters: set[str] = set()

    @property
    def enabled(self) -> bool:
        return bool(settings.ha_url and settings.ha_token)

    def on_state_changed(self, callback: Callable):
        """Register callback(entity_id, old_state, new_state)."""
        self._callbacks.append(callback)

    def filter_entities(self, entity_ids: list[str]):
        """Only fire callbacks for these entity_ids (empty = all)."""
        self._entity_filters.update(entity_ids)

    def start(self):
        """Start background listener task."""
        if not self.enabled:
            log.info("[ha_ws] HA not configured — WS listener disabled")
            return
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        log.info("[ha_ws] WebSocket listener started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self):
        try:
            import websockets
        except ImportError:
            log.warning("[ha_ws] websockets not installed: pip install websockets")
            return

        while self._running:
            try:
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    # Auth flow
                    hello = json.loads(await ws.recv())
                    if hello.get("type") == "auth_required":
                        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                        auth_result = json.loads(await ws.recv())
                        if auth_result.get("type") != "auth_ok":
                            log.error(f"[ha_ws] Auth failed: {auth_result}")
                            return

                    # Subscribe to state_changed events
                    sub_id = self._msg_id
                    self._msg_id += 1
                    await ws.send(json.dumps({
                        "id": sub_id,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }))
                    log.info("[ha_ws] Subscribed to state_changed events")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle(msg)
                        except Exception as e:
                            log.debug(f"[ha_ws] Parse error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    log.warning(f"[ha_ws] Disconnected ({e}), retrying in 5s…")
                    await asyncio.sleep(5)

    async def _handle(self, msg: dict):
        if msg.get("type") != "event":
            return
        event = msg.get("event", {})
        if event.get("event_type") != "state_changed":
            return

        data       = event.get("data", {})
        entity_id  = data.get("entity_id", "")
        old_state  = (data.get("old_state") or {}).get("state")
        new_state  = (data.get("new_state") or {}).get("state")

        if self._entity_filters and entity_id not in self._entity_filters:
            return

        if old_state != new_state:
            log.debug(f"[ha_ws] {entity_id}: {old_state} → {new_state}")
            for cb in self._callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        await cb(entity_id, old_state, new_state)
                    else:
                        cb(entity_id, old_state, new_state)
                except Exception as e:
                    log.error(f"[ha_ws] Callback error: {e}")


# ── Singleton ──────────────────────────────────────────────────────────────────

_ws_listener: Optional[HAWebSocketListener] = None


def get_ws_listener() -> HAWebSocketListener:
    global _ws_listener
    if _ws_listener is None:
        _ws_listener = HAWebSocketListener()
    return _ws_listener
