"""Deterministic resolution of explicit command goals and trivial room events.

No device is actuated here. Actual commands still go through the scoped tools.
"""

from dataclasses import replace
from datetime import timedelta

from fairy.apps.building_world.types import DeviceHealth, DeviceType, MeetingStatus

_COMMANDS = {
    DeviceType.HVAC: "HvacApp__set_hvac",
    DeviceType.VENTILATION: "VentilationApp__set_ventilation",
    DeviceType.HUMIDIFIER: "AirDeviceApp__set_humidifier",
    DeviceType.AIR_PURIFIER: "AirDeviceApp__set_air_purifier",
    DeviceType.LIGHTING: "LightingApp__set_lighting",
    DeviceType.PROJECTOR: "MeetingEquipmentApp__set_projector",
    DeviceType.AUDIO: "MeetingEquipmentApp__set_audio_system",
    DeviceType.PRINTER: "PrintingApp__set_printer_power",
}


def resolve_device_contract(world, operations, room_id, expected, verification):
    """Return verified success, a known policy blocker, or None for model work."""

    blockers = []
    planned_reduction = False
    if operations is not None:
        for goal in expected:
            spec = world.devices[goal["device_id"]]
            state = world.device_states[spec.device_id]
            projected = replace(state, settings=dict(state.settings))
            for key, value in goal.items():
                if key != "device_id":
                    setattr(projected, key, value)
            planned_reduction |= operations._estimated_device_power_w(
                spec, projected
            ) < operations._estimated_device_power_w(spec, state)
    for goal in expected:
        device_id = goal["device_id"]
        state = world.device_states[device_id]
        mismatched = any(
            getattr(state, key) != value
            for key, value in goal.items()
            if key != "device_id"
        )
        if mismatched and state.health != DeviceHealth.ONLINE:
            blockers.append({"reason": "device_unavailable", "device_id": device_id})
            continue
        command = _COMMANDS.get(world.devices[device_id].device_type)
        if operations is None or command is None:
            continue
        arguments = {
            "device_id": device_id,
            "power_on": goal.get("power_on", state.power_on),
            "mode": goal.get("mode", state.mode),
            "level": goal.get("level", state.level),
        }
        if command == "HvacApp__set_hvac":
            arguments["fan_level"] = arguments["level"]
        rejection = operations.preflight_room_device_command(command, arguments)
        # A worker may first shed load specified in this SAME contract. Do not
        # reject such a plan based on its initial load; each real tool rechecks.
        if rejection and not (
            rejection["reason"] == "power_budget_exceeded" and planned_reduction
        ):
            blockers.append(dict(rejection))
    if blockers:
        status = "blocked"
        retry_at = None
        if operations is not None and all(
            item["reason"] == "unoccupied_energy_risk" for item in blockers
        ):
            now = operations.runtime.current_time
            preparation_times = [
                meeting.start_at
                - timedelta(minutes=operations.preparation_lead_minutes)
                for meeting in world.schedule.values()
                if meeting.room_id == room_id
                and meeting.status == MeetingStatus.CONFIRMED
                and meeting.start_at
                - timedelta(minutes=operations.preparation_lead_minutes)
                > now
            ]
            if preparation_times:
                status = "deferred"
                retry_at = min(preparation_times).isoformat()
        return {
            "status": status,
            "reason": blockers[0]["reason"],
            "blockers": blockers,
            "retry_at": retry_at,
            "retry_on": "relevant_time_occupancy_schedule_or_power_change",
            "answer": "Requested device state is currently disallowed. Resolve the policy blocker or wait for retry_at; resetting the worker cannot bypass it.",
        }
    if verification["can_finish"]:
        return {
            "status": "ok",
            "reason": "device_postconditions_verified",
            "answer": "Requested device fields verified against canonical state. Physical comfort is outside this command-state contract.",
        }
    return None


def reconciled_empty_room(world, operations, room_id, events) -> bool:
    """Only known empty-room end/occupancy facts can be acknowledged without LLM."""

    if (
        operations is None
        or not events
        or any(
            item.get("event_type") not in {"occupancy_changed", "meeting_ended"}
            for item in events
        )
    ):
        return False
    state = world.room_states[room_id]
    if state.occupancy_count or state.pending_task_ids:
        return False
    if any(
        world.device_states[device.device_id].power_on
        for device in world.devices.values()
        if device.room_id == room_id
    ):
        return False
    now = operations.runtime.current_time
    if any(
        meeting.room_id == room_id
        and meeting.status == MeetingStatus.CONFIRMED
        and now < meeting.end_at
        and meeting.start_at
        <= now + timedelta(minutes=operations.preparation_lead_minutes)
        for meeting in world.schedule.values()
    ):
        return False
    return not any(
        request["status"] != "completed"
        for request in operations.printing.requests.values()
    )
