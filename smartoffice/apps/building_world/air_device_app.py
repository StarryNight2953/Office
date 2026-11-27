"""Humidifier and air-purifier command tools."""

from __future__ import annotations

from typing import Annotated, Any

from smartoffice.apps.app import App
from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.types import (
    BuildingEventType,
    DeviceHealth,
    DeviceType,
)
from smartoffice.tool_utils import OperationType, app_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check


class AirDeviceApp(App):
    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="AirDeviceApp")
        self.world = world

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_humidifier(
        self,
        device_id: str,
        power_on: bool,
        level: Annotated[int, {"minimum": 0, "maximum": 3}],
    ) -> dict[str, Any]:
        result = self._set_level_device(
            device_id, DeviceType.HUMIDIFIER, power_on, level
        )
        if result is not None:
            return result
        state = self.world.device_states[device_id]
        if power_on and state.water_level_pct <= 0.0:
            return _rejected("water_tank_empty")
        return self._commit(device_id, power_on, level)

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_air_purifier(
        self,
        device_id: str,
        power_on: bool,
        level: Annotated[int, {"minimum": 0, "maximum": 3}],
    ) -> dict[str, Any]:
        result = self._set_level_device(
            device_id, DeviceType.AIR_PURIFIER, power_on, level
        )
        if result is not None:
            return result
        state = self.world.device_states[device_id]
        if power_on and state.filter_life_pct <= 0.0:
            return _rejected("filter_expired")
        return self._commit(device_id, power_on, level)

    def _set_level_device(
        self,
        device_id: str,
        expected_type: DeviceType,
        power_on: bool,
        level: int,
    ) -> dict[str, Any] | None:
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None:
            return _rejected("unknown_device", device_id=device_id)
        if spec.device_type != expected_type:
            return _rejected("wrong_device_type", device_id=device_id)
        if state.health != DeviceHealth.ONLINE:
            return _rejected("device_unavailable", health=state.health.value)
        if power_on and level not in {1, 2, 3}:
            return _rejected("invalid_level")
        if not power_on and level not in {0, 1, 2, 3}:
            return _rejected("invalid_level")
        return None

    def _commit(self, device_id: str, power_on: bool, level: int) -> dict[str, Any]:
        state = self.world.device_states[device_id]
        state.power_on = bool(power_on)
        state.mode = "on" if power_on else "off"
        state.level = level if power_on else 0
        event = self.world.publish_building_event(
            BuildingEventType.DEVICE_STATE_CHANGED,
            source=self.name,
            subject_id=device_id,
            payload={"power_on": state.power_on, "level": state.level},
        )
        return {
            "status": "accepted",
            "device_id": device_id,
            "event_id": event.event_id,
            "message": "command accepted; verify physical effect using sensors",
        }


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **details}
