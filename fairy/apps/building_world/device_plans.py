"""Shared, feasible device-state contracts and guarded plan execution.

No physics or future events are consulted. A command-state contract is not a
physical comfort goal; observations and the ordinary time guards remain needed.
"""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Annotated, NotRequired, TypedDict

from fairy.apps.building_world.types import DeviceHealth, DeviceType


class DeviceExpectation(TypedDict):
    device_id: str
    power_on: NotRequired[bool]
    mode: NotRequired[
        Annotated[
            str,
            {
                "description": "Device-specific canonical mode: HVAC off/cooling/heating/fan; ventilation off/outdoor_air; humidifier/purifier off/on; projector/audio/printer off/ready. Lighting accepts its scene name. Unsupported modes are rejected before execution."
            },
        ]
    ]
    level: NotRequired[int]
    target_temperature_c: NotRequired[float]


class RoomTask(TypedDict):
    room_id: str
    task: str
    expected_devices: NotRequired[list[DeviceExpectation]]


COMMANDS = {
    DeviceType.HVAC: "HvacApp__set_hvac",
    DeviceType.VENTILATION: "VentilationApp__set_ventilation",
    DeviceType.HUMIDIFIER: "AirDeviceApp__set_humidifier",
    DeviceType.AIR_PURIFIER: "AirDeviceApp__set_air_purifier",
    DeviceType.LIGHTING: "LightingApp__set_lighting",
    DeviceType.PROJECTOR: "MeetingEquipmentApp__set_projector",
    DeviceType.AUDIO: "MeetingEquipmentApp__set_audio_system",
    DeviceType.PRINTER: "PrintingApp__set_printer_power",
}
FIXED_ON_MODES = {
    DeviceType.VENTILATION: "outdoor_air",
    DeviceType.HUMIDIFIER: "on",
    DeviceType.AIR_PURIFIER: "on",
    DeviceType.PROJECTOR: "ready",
    DeviceType.AUDIO: "ready",
    DeviceType.PRINTER: "ready",
}
MODE_ALIASES = {
    DeviceType.HVAC: {"cool": "cooling", "heat": "heating"},
    DeviceType.HUMIDIFIER: {"humidification": "on"},
    DeviceType.AIR_PURIFIER: {"purification": "on"},
    DeviceType.VENTILATION: {"on": "outdoor_air"},
}


def canonical_goals(world, room_id, goals):
    """Return a copied, normalized contract; reject impossible fields up front."""
    if goals is None:
        return None
    if not isinstance(goals, list) or not 1 <= len(goals) <= 64:
        raise ValueError("expected_devices must contain 1..64 device goals")
    result, seen = [], set()
    allowed = {"device_id", "power_on", "mode", "level", "target_temperature_c"}
    for item in goals:
        if not isinstance(item, dict) or set(item) - allowed or len(item) < 2:
            raise ValueError("unknown or missing device goal fields")
        device_id = item.get("device_id")
        spec = world.devices.get(device_id) if isinstance(device_id, str) else None
        if spec is None or spec.room_id != room_id or device_id in seen:
            raise ValueError("unknown, duplicate or foreign-room device")
        seen.add(device_id)
        goal = dict(item)
        kind = spec.device_type
        if kind not in COMMANDS:
            raise ValueError("unsupported device type")
        if "power_on" in goal and type(goal["power_on"]) is not bool:
            raise ValueError("power_on must be boolean")
        if "mode" in goal:
            if not isinstance(goal["mode"], str) or not goal["mode"]:
                raise ValueError("mode must be a nonempty string")
            goal["mode"] = MODE_ALIASES.get(kind, {}).get(goal["mode"], goal["mode"])
            modes = (
                {"off", "cooling", "heating", "fan"}
                if kind == DeviceType.HVAC
                else {"off", FIXED_ON_MODES[kind]}
                if kind in FIXED_ON_MODES
                else None
            )
            if modes is not None and goal["mode"] not in modes:
                raise ValueError(
                    f"unsupported mode for {kind.value}: choose {sorted(modes)}"
                )
            if goal.get("power_on") is False and goal["mode"] != "off":
                raise ValueError("powered-off devices require mode=off")
            if goal.get("power_on") is True and goal["mode"] == "off":
                raise ValueError("powered-on devices require an active mode")
        if "level" in goal:
            level = goal["level"]
            if type(level) is not int or not 0 <= level <= 3:
                raise ValueError("level must be an integer in 0..3")
            if goal.get("power_on") is False and level != 0:
                raise ValueError("powered-off devices require level=0")
            if goal.get("power_on") is True and level == 0:
                raise ValueError("powered-on devices require level>=1")
            if kind in {
                DeviceType.PROJECTOR,
                DeviceType.AUDIO,
                DeviceType.PRINTER,
            } and level not in {0, 1}:
                raise ValueError("this device supports only levels 0 and 1")
        if "target_temperature_c" in goal:
            value = goal["target_temperature_c"]
            if (
                kind != DeviceType.HVAC
                or type(value) not in {int, float}
                or not math.isfinite(value)
                or not 16 <= value <= 30
                or goal.get("power_on") is False
            ):
                raise ValueError(
                    "target_temperature_c requires an active HVAC target in 16..30"
                )
        if goal.get("mode") == "off" and (
            goal.get("level", 0) != 0 or "target_temperature_c" in goal
        ):
            raise ValueError(
                "mode=off cannot have an active level or temperature target"
            )
        if goal.get("level") == 0 and (
            goal.get("mode", "off") != "off" or "target_temperature_c" in goal
        ):
            raise ValueError("level=0 cannot have an active mode or temperature target")
        result.append(goal)
    return result


def mismatches(world, goals):
    return [
        {
            "code": "device_postcondition_failed",
            "device_id": goal["device_id"],
            "field": key,
            "expected": value,
            "actual": getattr(world.device_states[goal["device_id"]], key),
        }
        for goal in goals
        for key, value in goal.items()
        if key != "device_id"
        and getattr(world.device_states[goal["device_id"]], key) != value
    ]


def command_for_goal(world, goal, target_temperature_c):
    """Compile only explicit power goals; underspecified HVAC modes need a model."""
    if "power_on" not in goal:
        return None
    device_id = goal["device_id"]
    spec, state = world.devices[device_id], world.device_states[device_id]
    kind, power = spec.device_type, goal["power_on"]
    level = goal.get("level", max(1, state.level)) if power else 0
    args = {"device_id": device_id, "power_on": power}
    if kind == DeviceType.HVAC:
        mode = goal.get("mode", state.mode) if power else "off"
        if power and mode not in {"cooling", "heating", "fan"}:
            return None
        args.update(
            mode=mode,
            target_temperature_c=goal.get(
                "target_temperature_c",
                state.target_temperature_c or target_temperature_c,
            ),
            fan_level=level,
        )
    elif kind in {
        DeviceType.HUMIDIFIER,
        DeviceType.AIR_PURIFIER,
        DeviceType.VENTILATION,
    }:
        args["level"] = level
    elif kind == DeviceType.LIGHTING:
        prior_scene = state.settings.get("scene")
        brightness = state.settings.get("brightness_pct", 0)
        if not power:
            brightness = 0
        elif not 0 < brightness <= 100 or ("level" in goal and level != state.level):
            brightness = {1: 33, 2: 66, 3: 100}[level]
        args.update(
            brightness_pct=brightness,
            color_temperature_k=state.settings.get("color_temperature_k") or 4000,
            scene=goal.get(
                "mode",
                prior_scene if prior_scene and prior_scene != "off" else "default",
            )
            if power
            else "off",
        )
    elif kind == DeviceType.PROJECTOR:
        args["input_source"] = state.settings.get("input_source") or "HDMI1"
    elif kind == DeviceType.AUDIO:
        args.update(
            volume_pct=state.settings.get("volume_pct") or 50,
            microphone_enabled=state.settings.get("microphone_enabled", True),
        )
    projected = replace(state, settings=dict(state.settings))
    projected.power_on = power
    projected.level = (
        (
            1
            if kind in {DeviceType.PROJECTOR, DeviceType.AUDIO, DeviceType.PRINTER}
            else level
        )
        if power
        else 0
    )
    projected.mode = (
        args.get("mode", goal.get("mode", FIXED_ON_MODES.get(kind, projected.mode)))
        if power
        else "off"
    )
    if kind == DeviceType.LIGHTING:
        projected.settings["brightness_pct"] = args["brightness_pct"]
    return COMMANDS[kind], args, projected


def execute_device_plan(operations, room_id, goals, invoke, before_action=None, actor="common_plan"):
    """Validate entire plan, shed load first, then guard and audit every command.

    Commands are not transactional: if a live command fails, report partial work
    honestly, stop, and never claim the whole plan succeeded.
    """
    world = operations.world
    plan = []
    policy_checks = []
    for goal in goals:
        needs_action = bool(mismatches(world, [goal]))
        if needs_action and "power_on" not in goal:
            return {
                "status": "rejected",
                "reason": "device_plan_requires_model",
                "actions": [],
            }
        policy_goal = {
            "power_on": world.device_states[goal["device_id"]].power_on,
            **goal,
        }
        command = command_for_goal(world, policy_goal, operations.target_temperature_c)
        if command is None:
            if not needs_action:
                continue
            return {
                "status": "rejected",
                "reason": "device_plan_requires_model",
                "actions": [],
            }
        name, args, projected = command
        policy_checks.append((name, args))
        if not needs_action:
            continue
        if name not in operations.device_command_tools:
            return {
                "status": "rejected",
                "reason": "device_tool_unavailable",
                "actions": [],
            }
        if world.device_states[goal["device_id"]].health != DeviceHealth.ONLINE:
            return {"status": "blocked", "reason": "device_unavailable", "actions": []}
        delta = operations._estimated_device_power_w(
            world.devices[goal["device_id"]], projected
        ) - operations._estimated_device_power_w(
            world.devices[goal["device_id"]], world.device_states[goal["device_id"]]
        )
        plan.append((delta, name, args))
    # Static policy failures must not leave half of an intrinsically forbidden
    # plan applied. Shared-power feasibility is rechecked after each reduction.
    for name, args in policy_checks:
        rejection = operations.preflight_room_device_command(name, args)
        if rejection and rejection["reason"] != "power_budget_exceeded":
            return {
                "status": "blocked",
                "reason": rejection["reason"],
                "blockers": [rejection],
                "actions": [],
            }
    actions = []
    for _, name, args in sorted(plan, key=lambda item: item[0]):
        rejection = operations.preflight_room_device_command(name, args)
        if rejection:
            return {
                "status": "blocked",
                "reason": rejection["reason"],
                "blockers": [rejection],
                "actions": actions,
            }
        if before_action is not None:
            before_action()
        started = time.perf_counter()
        try:
            result = invoke(name, args)
        except Exception as exc:  # noqa: BLE001 - gateway isolation retains attempted actions
            result = {
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        record = {
            "action_id": len(operations.device_plan_actions) + 1,
            "actor": actor,
            "simulation_time": operations.runtime.current_time.isoformat(),
            "room_id": room_id,
            "tool_name": name,
            "tool_args": args,
            "result": result,
            "wall_time_seconds": time.perf_counter() - started,
        }
        operations.device_plan_actions.append(record)
        actions.append(record)
        if not isinstance(result, dict) or result.get("status") != "accepted":
            return {
                "status": "blocked",
                "reason": "device_plan_command_failed",
                "actions": actions,
            }
    failures = mismatches(world, goals)
    return {
        "status": "ok" if not failures else "blocked",
        "reason": "device_postconditions_verified"
        if not failures
        else "device_plan_verification_failed",
        "actions": actions,
        "blockers": failures,
        "verified": not failures,
        "scope": "command_state_only; physical comfort requires observation",
    }


def coalesce_room_tasks(tasks):
    """Merge compatible explicit goals; never guess how to merge prose-only work."""
    grouped = {}
    for task in tasks:
        room = task["room_id"]
        if room not in grouped:
            grouped[room] = {
                **task,
                "expected_devices": [dict(g) for g in task["expected_devices"]]
                if task.get("expected_devices")
                else None,
            }
            continue
        current = grouped[room]
        if not current["expected_devices"] or not task.get("expected_devices"):
            raise ValueError("duplicate_room_id")
        devices = {g["device_id"]: g for g in current["expected_devices"]}
        for goal in task["expected_devices"]:
            existing = devices.setdefault(goal["device_id"], {})
            if any(k in existing and existing[k] != v for k, v in goal.items()):
                raise ValueError("conflicting_room_goals")
            existing.update(goal)
        current["expected_devices"] = list(devices.values())
        current["task"] += "\n" + task["task"]
    return list(grouped.values())
