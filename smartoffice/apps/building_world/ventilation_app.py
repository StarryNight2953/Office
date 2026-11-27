"""Dedicated outdoor-air ventilation controls for Building World."""

from __future__ import annotations

from typing import Annotated, Any

from smartoffice.apps.app import App
from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.types import BuildingEventType, DeviceHealth, DeviceType
from smartoffice.tool_utils import OperationType, app_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check


class VentilationApp(App):
    """Command ventilation; Runtime converts the command into outdoor airflow."""

    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="VentilationApp")
        self.world = world

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_ventilation(
        self,
        device_id: str,
        power_on: bool,
        level: Annotated[int, {"minimum": 0, "maximum": 3}],
    ) -> dict[str, Any]:
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None:
            return _rejected("unknown_device", device_id=device_id)
        if spec.device_type != DeviceType.VENTILATION:
            return _rejected("wrong_device_type", device_id=device_id)
        if state.health != DeviceHealth.ONLINE:
            return _rejected("device_unavailable", health=state.health.value)
        if power_on and level not in {1, 2, 3}:
            return _rejected("invalid_level")
        if not power_on and level not in {0, 1, 2, 3}:
            return _rejected("invalid_level")

        state.power_on = power_on
        state.mode = "outdoor_air" if power_on else "off"
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
            "message": "command accepted; verify CO2 response after advancing time",
        }


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **details}
