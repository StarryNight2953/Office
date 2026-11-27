"""Device discovery and health boundary for Building World."""

from __future__ import annotations

from typing import Any

from smartoffice.apps.app import App
from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.types import DeviceHealth, DeviceType
from smartoffice.tool_utils import OperationType, app_tool, data_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check


class DeviceRegistryApp(App):
    """Expose registered capabilities without letting callers mutate specs."""

    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="DeviceRegistryApp")
        self.world = world

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def list_devices(
        self, zone_id: str | None = None, device_type: str | None = None
    ) -> dict[str, Any]:
        if zone_id is not None and zone_id not in self.world.zones:
            return {"error": f"unknown zone_id {zone_id!r}"}
        parsed_type = None
        if device_type is not None:
            try:
                parsed_type = DeviceType(device_type)
            except ValueError:
                return {"error": f"unsupported device_type {device_type!r}"}
        devices = []
        for spec in self.world.devices.values():
            if zone_id is not None and spec.zone_id != zone_id:
                continue
            if parsed_type is not None and spec.device_type != parsed_type:
                continue
            state = self.world.device_states[spec.device_id]
            devices.append(
                {
                    "device_id": spec.device_id,
                    "device_type": spec.device_type.value,
                    "room_id": spec.room_id,
                    "zone_id": spec.zone_id,
                    "capabilities": sorted(spec.capabilities),
                    "rated_power_w": spec.rated_power_w,
                    "available": state.health == DeviceHealth.ONLINE,
                    "health": state.health.value,
                    "power_on": state.power_on,
                    "mode": state.mode,
                    "level": state.level,
                }
            )
        return {"devices": devices}

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_device_state(self, device_id: str) -> dict[str, Any]:
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None:
            return {"error": f"unknown device_id {device_id!r}"}
        return {
            "device_id": device_id,
            "device_type": spec.device_type.value,
            "zone_id": spec.zone_id,
            "power_on": state.power_on,
            "mode": state.mode,
            "level": state.level,
            "target_temperature_c": state.target_temperature_c,
            "health": state.health.value,
            "water_level_pct": state.water_level_pct,
            "filter_life_pct": state.filter_life_pct,
        }
