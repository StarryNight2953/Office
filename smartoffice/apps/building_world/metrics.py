"""Cross-scenario operational metrics for Building World experiments."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from smartoffice.apps.building_world.types import BuildingEventType

if TYPE_CHECKING:
    from smartoffice.apps.building_world.runtime import BuildingWorldRuntime


def build_building_metrics(runtime: BuildingWorldRuntime) -> dict[str, Any]:
    """Summarize physics, events and resource state using one stable schema."""

    physics_steps = [
        item for item in runtime.trace if item.get("kind") == "physics_step"
    ]
    building_power_w = [
        sum(float(zone.get("electric_power_w", 0.0)) for zone in step.get("zones", []))
        for step in physics_steps
    ]
    zone_accumulators: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "simulated_seconds": 0.0,
            "comfort_violation_seconds": 0.0,
            "occupied_seconds": 0.0,
            "occupied_comfort_violation_seconds": 0.0,
            "air_quality_violation_seconds": 0.0,
            "unoccupied_energy_kwh": 0.0,
            "peak_co2_ppm": 0.0,
            "peak_pm25_ug_m3": 0.0,
            "minimum_comfort_score": 1.0,
        }
    )
    for step in physics_steps:
        timestep = float(step.get("timestep_seconds", 0.0))
        for zone in step.get("zones", []):
            zone_id = str(zone["zone_id"])
            values = zone_accumulators[zone_id]
            comfort = float(zone["comfort_score"])
            co2 = float(zone["co2_ppm"])
            pm25 = float(zone["pm25_ug_m3"])
            occupancy = int(zone.get("occupancy_count", 0))
            values["simulated_seconds"] += timestep
            if comfort < 0.7:
                values["comfort_violation_seconds"] += timestep
            if occupancy > 0:
                values["occupied_seconds"] += timestep
                if comfort < 0.7:
                    values["occupied_comfort_violation_seconds"] += timestep
            else:
                values["unoccupied_energy_kwh"] += float(zone["energy_kwh"])
            if co2 >= 1000.0 or pm25 >= 35.0:
                values["air_quality_violation_seconds"] += timestep
            values["peak_co2_ppm"] = max(values["peak_co2_ppm"], co2)
            values["peak_pm25_ug_m3"] = max(values["peak_pm25_ug_m3"], pm25)
            values["minimum_comfort_score"] = min(
                values["minimum_comfort_score"], comfort
            )

    wake_decisions = [
        item for item in runtime.trace if item.get("kind") == "trigger_decision"
    ]
    events_by_type: dict[str, int] = defaultdict(int)
    for event in runtime.world.events:
        events_by_type[event.event_type.value] += 1
    device_actions_by_type: dict[str, int] = defaultdict(int)
    for event in runtime.world.events:
        if event.event_type != BuildingEventType.DEVICE_STATE_CHANGED:
            continue
        device = runtime.world.devices.get(event.subject_id)
        device_type = device.device_type.value if device else "unknown"
        device_actions_by_type[device_type] += 1

    active_devices = [
        device_id
        for device_id, state in runtime.world.device_states.items()
        if state.power_on
    ]
    active_reservations = [
        reservation.reservation_id
        for reservation in runtime.world.reservations.values()
        if reservation.status.value == "active"
    ]
    shadow = runtime.sensor_shadow.summary() if runtime.sensor_shadow else None
    return {
        "schema_version": 1,
        "simulated_time_seconds": sum(
            float(item.get("timestep_seconds", 0.0)) for item in physics_steps
        ),
        "physics_step_count": len(physics_steps),
        "energy": {
            "total_kwh": sum(runtime.physics.cumulative_energy_kwh_by_zone.values()),
            "by_zone_kwh": dict(
                sorted(runtime.physics.cumulative_energy_kwh_by_zone.items())
            ),
            "peak_power_w": max(building_power_w, default=0.0),
        },
        "environment": {
            "by_zone": [
                {"zone_id": zone_id, **values}
                for zone_id, values in sorted(zone_accumulators.items())
            ]
        },
        "events": {
            "total": len(runtime.world.events),
            "by_type": dict(sorted(events_by_type.items())),
            "trigger_decisions": len(wake_decisions),
            "agent_wake_count": sum(
                bool(item.get("should_wake_agent")) for item in wake_decisions
            ),
        },
        "actions": {
            "device_state_change_count": sum(device_actions_by_type.values()),
            "by_device_type": dict(sorted(device_actions_by_type.items())),
        },
        "resources": {
            "active_device_ids": sorted(active_devices),
            "active_reservation_ids": sorted(active_reservations),
        },
        "shadow": shadow,
        "inventory": {
            "room_count": len(runtime.world.rooms),
            "zone_count": len(runtime.world.zones),
            "device_count": len(runtime.world.devices),
            "sensor_count": len(
                runtime.physics.observation_model.sensor_specs
                if runtime.physics.observation_model is not None
                else ()
            ),
            "device_types": sorted(
                {device.device_type.value for device in runtime.world.devices.values()}
            ),
        },
    }
