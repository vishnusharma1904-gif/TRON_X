"""
TRON-X IoT API  --  Phase 16 (extended)

Routes at /api/iot:

  Core (existing)
    GET  /status
    GET  /states
    GET  /states/{entity_id}
    GET  /lights
    GET  /switches
    GET  /sensors
    GET  /summary
    POST /service
    POST /turn_on/{entity_id}
    POST /turn_off/{entity_id}
    POST /toggle/{entity_id}
    POST /light/color
    POST /thermostat
    POST /command              (NL)

  MQTT  (Phase 16)
    GET  /mqtt/status
    POST /mqtt/connect
    POST /mqtt/publish
    POST /mqtt/subscribe
    DELETE /mqtt/subscribe/{topic}
    GET  /mqtt/topics
    GET  /mqtt/history/{topic}

  Scenes  (Phase 16)
    GET  /scenes
    POST /scenes/{scene_id}/activate
    POST /scenes/apply
    POST /scenes/create

  Scripts  (Phase 16)
    GET  /scripts
    POST /scripts/{script_id}/run

  Automations  (Phase 16)
    GET  /automations
    POST /automations/{automation_id}/trigger
    POST /automations/{automation_id}/enable
    POST /automations/{automation_id}/disable

  Device Groups  (Phase 16)
    GET  /groups
    POST /groups
    GET  /groups/{name}
    DELETE /groups/{name}
    POST /groups/{name}/on
    POST /groups/{name}/off
    POST /groups/{name}/toggle
    POST /groups/{name}/service
    GET  /groups/{name}/status
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.iot.home_assistant import get_ha
from src.iot.nl_mapper      import nl_to_ha_command
from src.iot.device_groups  import get_device_groups

router = APIRouter(prefix="/api/iot", tags=["iot"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ServiceCallReq(BaseModel):
    domain:    str
    service:   str
    entity_id: Optional[str] = None
    extra:     Optional[dict] = None

class LightReq(BaseModel):
    entity_id:  str
    r:          Optional[int] = None
    g:          Optional[int] = None
    b:          Optional[int] = None
    brightness: Optional[int] = None

class ThermostatReq(BaseModel):
    entity_id:   str
    temperature: float

class NLCommandReq(BaseModel):
    command: str
    persona: str  = "jarvis"
    execute: bool = Field(default=True, description="Actually execute the mapped command")

# MQTT schemas
class MQTTConnectReq(BaseModel):
    host:     str           = "localhost"
    port:     int           = 1883
    username: Optional[str] = None
    password: Optional[str] = None

class MQTTPublishReq(BaseModel):
    topic:   str
    payload: Any
    qos:     int  = Field(default=0, ge=0, le=2)
    retain:  bool = False

class MQTTSubscribeReq(BaseModel):
    topic: str

# Scene schemas
class ApplySceneReq(BaseModel):
    entities: Dict[str, dict] = Field(..., description='{"light.bedroom": {"state": "on", "brightness": 200}}')

class CreateSceneReq(BaseModel):
    scene_id:  str
    entities:  Dict[str, dict]
    snapshot:  bool = False

# Script schema
class RunScriptReq(BaseModel):
    variables: Optional[dict] = None

# Device group schemas
class CreateGroupReq(BaseModel):
    name:        str
    entities:    List[str]
    description: str = ""

class UpdateGroupReq(BaseModel):
    entities:    Optional[List[str]] = None
    description: Optional[str]       = None

class GroupServiceReq(BaseModel):
    service: str
    domain:  Optional[str] = None
    extra:   Optional[dict] = None


# ---------------------------------------------------------------------------
# Core  (existing endpoints preserved)
# ---------------------------------------------------------------------------

@router.get("/status")
async def iot_status():
    ha     = get_ha()
    online = await ha.ping()
    try:
        from src.iot.mqtt_client import get_mqtt
        mqtt_stats = get_mqtt().stats()
    except Exception:
        mqtt_stats = {"connected": False}
    return {
        "home_assistant": {
            "configured": ha.enabled,
            "online":     online,
        },
        "mqtt": mqtt_stats,
        "device_groups": {
            "count": len(get_device_groups().list_groups()),
        },
    }

@router.get("/states")
async def get_all_states():
    return await get_ha().get_states()

@router.get("/states/{entity_id:path}")
async def get_entity_state(entity_id: str):
    state = await get_ha().get_state(entity_id)
    if state is None:
        raise HTTPException(404, f"Entity '{entity_id}' not found")
    return state

@router.get("/lights")
async def get_lights():
    return await get_ha().get_all_lights()

@router.get("/switches")
async def get_switches():
    return await get_ha().get_all_switches()

@router.get("/sensors")
async def get_sensors():
    return await get_ha().get_all_sensors()

@router.get("/summary")
async def home_summary():
    return {"summary": await get_ha().home_summary()}

@router.post("/service")
async def call_service(req: ServiceCallReq):
    ha = get_ha()
    if not ha.enabled:
        raise HTTPException(503, "Home Assistant not configured")
    return await ha.call_service(req.domain, req.service, req.entity_id, req.extra)

@router.post("/turn_on/{entity_id:path}")
async def turn_on(entity_id: str):
    return await get_ha().turn_on(entity_id)

@router.post("/turn_off/{entity_id:path}")
async def turn_off(entity_id: str):
    return await get_ha().turn_off(entity_id)

@router.post("/toggle/{entity_id:path}")
async def toggle(entity_id: str):
    return await get_ha().toggle(entity_id)

@router.post("/light/color")
async def set_light_color(req: LightReq):
    return await get_ha().set_light_color(
        req.entity_id,
        req.r or 255, req.g or 255, req.b or 255,
        req.brightness or 255,
    )

@router.post("/thermostat")
async def set_thermostat(req: ThermostatReq):
    return await get_ha().set_thermostat(req.entity_id, req.temperature)

@router.post("/command")
async def natural_language_command(req: NLCommandReq):
    ha     = get_ha()
    ha_ctx = await ha.home_summary() if ha.enabled else ""
    cmd    = await nl_to_ha_command(req.command, ha_ctx, req.persona)

    if not cmd.get("domain"):
        return {
            "understood": False,
            "command":    req.command,
            "message":    "Could not map that to a Home Assistant command.",
        }

    result = {"understood": True, "mapped": cmd}
    if req.execute and ha.enabled:
        result["executed"] = await ha.call_service(
            domain=cmd["domain"],
            service=cmd["service"],
            entity_id=cmd.get("entity_id"),
            extra=cmd.get("extra") or None,
        )
    elif req.execute:
        result["executed"] = {"success": False, "error": "Home Assistant not configured"}
    else:
        result["dry_run"] = True
    return result


# ---------------------------------------------------------------------------
# MQTT  (Phase 16)
# ---------------------------------------------------------------------------

@router.get("/mqtt/status")
async def mqtt_status():
    from src.iot.mqtt_client import get_mqtt
    return get_mqtt().stats()

@router.post("/mqtt/connect")
async def mqtt_connect(req: MQTTConnectReq):
    from src.iot.mqtt_client import MQTTClient
    import src.iot.mqtt_client as _mc
    _mc._mqtt = MQTTClient(
        host=req.host, port=req.port,
        username=req.username, password=req.password,
    )
    ok = _mc._mqtt.connect()
    return {"success": ok, "broker": f"{req.host}:{req.port}"}

@router.post("/mqtt/publish")
async def mqtt_publish(req: MQTTPublishReq):
    from src.iot.mqtt_client import get_mqtt
    ok = get_mqtt().publish(req.topic, req.payload, req.qos, req.retain)
    if not ok:
        raise HTTPException(503, "MQTT not connected or publish failed")
    return {"success": True, "topic": req.topic}

@router.post("/mqtt/subscribe")
async def mqtt_subscribe(req: MQTTSubscribeReq):
    from src.iot.mqtt_client import get_mqtt
    ok = get_mqtt().subscribe(req.topic)
    return {"success": ok, "topic": req.topic}

@router.delete("/mqtt/subscribe/{topic:path}")
async def mqtt_unsubscribe(topic: str):
    from src.iot.mqtt_client import get_mqtt
    ok = get_mqtt().unsubscribe(topic)
    if not ok:
        raise HTTPException(404, f"Not subscribed to topic '{topic}'")
    return {"success": True, "topic": topic}

@router.get("/mqtt/topics")
async def mqtt_topics():
    from src.iot.mqtt_client import get_mqtt
    topics = get_mqtt().list_topics()
    return {"topics": topics, "count": len(topics)}

@router.get("/mqtt/history/{topic:path}")
async def mqtt_history(
    topic: str,
    limit: int = Query(default=20, ge=1, le=50),
):
    from src.iot.mqtt_client import get_mqtt
    history = get_mqtt().get_topic_history(topic, limit)
    return {"topic": topic, "messages": history, "count": len(history)}


# ---------------------------------------------------------------------------
# Scenes  (Phase 16)
# ---------------------------------------------------------------------------

@router.get("/scenes")
async def list_scenes():
    return {"scenes": await get_ha().get_scenes()}

@router.post("/scenes/{scene_id}/activate")
async def activate_scene(scene_id: str):
    result = await get_ha().activate_scene(scene_id)
    if not result.get("success"):
        raise HTTPException(502, result.get("error", "Failed to activate scene"))
    return result

@router.post("/scenes/apply")
async def apply_scene(req: ApplySceneReq):
    return await get_ha().apply_scene(req.entities)

@router.post("/scenes/create")
async def create_scene(req: CreateSceneReq):
    return await get_ha().create_scene(req.scene_id, req.entities, req.snapshot)


# ---------------------------------------------------------------------------
# Scripts  (Phase 16)
# ---------------------------------------------------------------------------

@router.get("/scripts")
async def list_scripts():
    return {"scripts": await get_ha().get_scripts()}

@router.post("/scripts/{script_id}/run")
async def run_script(script_id: str, req: Optional[RunScriptReq] = None):
    variables = req.variables if req else None
    result    = await get_ha().run_script(script_id, variables)
    if not result.get("success"):
        raise HTTPException(502, result.get("error", "Failed to run script"))
    return result


# ---------------------------------------------------------------------------
# Automations  (Phase 16)
# ---------------------------------------------------------------------------

@router.get("/automations")
async def list_automations():
    return {"automations": await get_ha().get_automations()}

@router.post("/automations/{automation_id}/trigger")
async def trigger_automation(automation_id: str):
    return await get_ha().trigger_automation(automation_id)

@router.post("/automations/{automation_id}/enable")
async def enable_automation(automation_id: str):
    return await get_ha().enable_automation(automation_id)

@router.post("/automations/{automation_id}/disable")
async def disable_automation(automation_id: str):
    return await get_ha().disable_automation(automation_id)


# ---------------------------------------------------------------------------
# Device Groups  (Phase 16)
# ---------------------------------------------------------------------------

@router.get("/groups")
async def list_groups():
    groups = get_device_groups().list_groups()
    return {"groups": groups, "count": len(groups)}

@router.post("/groups")
async def create_group(req: CreateGroupReq):
    result = get_device_groups().create_group(req.name, req.entities, req.description)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result

@router.get("/groups/{name}")
async def get_group(name: str):
    group = get_device_groups().get_group(name)
    if not group:
        raise HTTPException(404, f"Group '{name}' not found")
    return group

@router.patch("/groups/{name}")
async def update_group(name: str, req: UpdateGroupReq):
    result = get_device_groups().update_group(name, req.entities, req.description)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.delete("/groups/{name}")
async def delete_group(name: str):
    result = get_device_groups().delete_group(name)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.post("/groups/{name}/on")
async def group_on(name: str):
    result = await get_device_groups().group_on(name)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.post("/groups/{name}/off")
async def group_off(name: str):
    result = await get_device_groups().group_off(name)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.post("/groups/{name}/toggle")
async def group_toggle(name: str):
    result = await get_device_groups().group_toggle(name)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.post("/groups/{name}/service")
async def group_service(name: str, req: GroupServiceReq):
    result = await get_device_groups().control_group(
        name, req.service, req.domain, req.extra
    )
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result

@router.get("/groups/{name}/status")
async def group_status(name: str):
    result = await get_device_groups().group_status(name)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result
