"""
TRON-X MQTT Client  --  Phase 16 (extended)

Mosquitto/Zigbee2MQTT integration via paho-mqtt (async wrapper).
Phase 16 additions:
  - Per-topic message history ring buffer (last 50 messages each)
  - Topic discovery: list active subscriptions with last-seen payload
  - API-driven subscribe / unsubscribe
  - stats() for health endpoint
  - Total message counters
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from src.core.config import settings
from src.core.logger import log

_MAX_HISTORY = 50   # messages retained per topic


def _check_paho() -> bool:
    try:
        import paho.mqtt.client as mqtt  # noqa: F401
        return True
    except ImportError:
        return False


class MQTTClient:
    """
    Async-friendly wrapper around paho-mqtt.
    Runs the paho network loop in a background thread.
    """

    def __init__(
        self,
        host:     str           = "localhost",
        port:     int           = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self._host     = host
        self._port     = port
        self._username = username
        self._password = password
        self._client   = None
        self._connected = False

        # subscriptions: topic -> list of callbacks
        self._subscriptions: dict[str, list[Callable]] = {}

        # Phase 16: per-topic ring buffer + counters
        self._history:        dict[str, deque]   = {}   # topic -> deque[{ts, payload}]
        self._message_counts: dict[str, int]     = {}   # topic -> total received
        self._total_received: int                = 0
        self._total_published: int               = 0
        self._connected_at:   Optional[float]    = None

        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -----------------------------------------------------------------------
    # Connection
    # -----------------------------------------------------------------------

    def connect(self) -> bool:
        if not _check_paho():
            log.warning("[mqtt] paho-mqtt not installed: pip install paho-mqtt")
            return False
        try:
            import paho.mqtt.client as mqtt

            self._client = mqtt.Client()
            if self._username:
                self._client.username_pw_set(self._username, self._password)

            self._client.on_connect    = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message    = self._on_message

            self._client.connect(self._host, self._port, keepalive=60)
            self._loop = asyncio.get_event_loop()

            thread = threading.Thread(target=self._client.loop_forever, daemon=True)
            thread.start()

            log.info("[mqtt] Connecting to %s:%d", self._host, self._port)
            return True
        except Exception as e:
            log.error("[mqtt] Connect failed: %s", e)
            return False

    def disconnect(self):
        if self._client:
            self._client.disconnect()
        self._connected    = False
        self._connected_at = None

    # -----------------------------------------------------------------------
    # paho callbacks
    # -----------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        self._connected = rc == 0
        if self._connected:
            self._connected_at = time.time()
            log.info("[mqtt] Connected to %s:%d", self._host, self._port)
            for topic in self._subscriptions:
                client.subscribe(topic)
        else:
            log.warning("[mqtt] Connection refused (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected    = False
        self._connected_at = None
        log.warning("[mqtt] Disconnected (rc=%d)", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode("utf-8")
        except Exception:
            payload = str(msg.payload)

        # -- Phase 16: history buffer -----------------------------------------
        topic = msg.topic
        if topic not in self._history:
            self._history[topic] = deque(maxlen=_MAX_HISTORY)
        self._history[topic].append({
            "ts":      time.time(),
            "payload": payload,
        })
        self._message_counts[topic] = self._message_counts.get(topic, 0) + 1
        self._total_received += 1
        # ---------------------------------------------------------------------

        callbacks = self._subscriptions.get(topic, [])
        # Also check wildcard subscriptions
        for sub_topic, cbs in self._subscriptions.items():
            if sub_topic.endswith("#"):
                prefix = sub_topic[:-1]
                if topic.startswith(prefix) and sub_topic != topic:
                    callbacks = callbacks + cbs

        if self._loop and self._loop.is_running():
            for cb in callbacks:
                asyncio.run_coroutine_threadsafe(
                    self._dispatch(cb, topic, payload),
                    self._loop,
                )
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._message_queue.put({"topic": topic, "payload": payload}),
                self._loop,
            )

    async def _dispatch(self, cb: Callable, topic: str, payload: str):
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb(topic, payload)
            else:
                cb(topic, payload)
        except Exception as e:
            log.error("[mqtt] Callback error: %s", e)

    # -----------------------------------------------------------------------
    # Public API -- publish / subscribe
    # -----------------------------------------------------------------------

    def publish(
        self,
        topic:   str,
        payload: Any,
        qos:     int  = 0,
        retain:  bool = False,
    ) -> bool:
        if not self._connected or not self._client:
            log.warning("[mqtt] Not connected -- cannot publish to %s", topic)
            return False
        msg = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        result = self._client.publish(topic, msg, qos=qos, retain=retain)
        if result.rc == 0:
            self._total_published += 1
            log.debug("[mqtt] Publish %s -> %s", topic, msg[:80])
        return result.rc == 0

    def subscribe(self, topic: str, callback: Optional[Callable] = None) -> bool:
        if not self._client:
            return False
        if topic not in self._subscriptions:
            self._subscriptions[topic] = []
            if self._history.get(topic) is None:
                self._history[topic] = deque(maxlen=_MAX_HISTORY)
            if self._connected:
                self._client.subscribe(topic)
        if callback:
            self._subscriptions[topic].append(callback)
        log.info("[mqtt] Subscribed to %s", topic)
        return True

    def unsubscribe(self, topic: str) -> bool:
        """Remove a topic subscription."""
        if topic not in self._subscriptions:
            return False
        self._subscriptions.pop(topic, None)
        if self._client and self._connected:
            self._client.unsubscribe(topic)
        log.info("[mqtt] Unsubscribed from %s", topic)
        return True

    async def receive(self, timeout: float = 5.0) -> Optional[dict]:
        """Wait for next MQTT message from any subscribed topic."""
        try:
            return await asyncio.wait_for(self._message_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    # -----------------------------------------------------------------------
    # Phase 16: topic discovery + history
    # -----------------------------------------------------------------------

    def list_topics(self) -> list[dict]:
        """
        List all subscribed topics with their last message and message count.

        Returns:
            [
              {
                "topic":         str,
                "subscribed":    bool,
                "message_count": int,
                "last_message":  str | None,
                "last_ts":       float | None,   # unix timestamp
              }
            ]
        """
        result = []
        for topic in self._subscriptions:
            hist = self._history.get(topic)
            last = hist[-1] if hist else None
            result.append({
                "topic":         topic,
                "subscribed":    True,
                "message_count": self._message_counts.get(topic, 0),
                "last_message":  last["payload"] if last else None,
                "last_ts":       last["ts"]      if last else None,
            })
        return result

    def get_topic_history(self, topic: str, limit: int = 50) -> list[dict]:
        """
        Return last N messages for a topic.

        Returns:
            [{"ts": float, "payload": str}, ...]  newest-first
        """
        hist = self._history.get(topic)
        if not hist:
            return []
        limit = max(1, min(limit, _MAX_HISTORY))
        return list(reversed(list(hist)))[:limit]

    def stats(self) -> dict:
        """Return a summary dict suitable for the /status endpoint."""
        uptime = round(time.time() - self._connected_at, 1) if self._connected_at else None
        return {
            "connected":        self._connected,
            "broker":           f"{self._host}:{self._port}",
            "topic_count":      len(self._subscriptions),
            "total_received":   self._total_received,
            "total_published":  self._total_published,
            "uptime_s":         uptime,
        }

    # -----------------------------------------------------------------------
    # Zigbee2MQTT helpers
    # -----------------------------------------------------------------------

    def z2m_set(self, device: str, payload: dict) -> bool:
        return self.publish(f"zigbee2mqtt/{device}/set", payload)

    def z2m_get(self, device: str) -> bool:
        return self.publish(f"zigbee2mqtt/{device}/get", {"state": ""})

    def z2m_subscribe_device(self, device: str,
                              callback: Optional[Callable] = None) -> bool:
        return self.subscribe(f"zigbee2mqtt/{device}", callback)

    def z2m_subscribe_all(self, callback: Optional[Callable] = None) -> bool:
        return self.subscribe("zigbee2mqtt/#", callback)

    @property
    def is_connected(self) -> bool:
        return self._connected


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_mqtt: Optional[MQTTClient] = None


def get_mqtt(host: str = "localhost", port: int = 1883) -> MQTTClient:
    global _mqtt
    if _mqtt is None:
        _mqtt = MQTTClient(host=host, port=port)
    return _mqtt
