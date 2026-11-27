"""Room lighting controls with scene and dimming state."""

from __future__ import annotations

from typing import Annotated, Any

from smartoffice.apps.app import App
from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.types import BuildingEventType, DeviceHealth, DeviceType
from smartoffice.tool_utils import OperationType, app_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check


class LightingApp(App):
    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="LightingApp")
        self.world = world

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_lighting(
        self,
        device_id: str,
        power_on: bool,
        brightness_pct: Annotated[int, {"minimum": 0, "maximum": 100}],
        color_temperature_k: Annotated[int, {"minimum": 2700, "maximum": 6500}],
        scene: str,
    ) -> dict[str, Any]:
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None:
            return _rejected("unknown_device", device_id=device_id)
        if spec.device_type != DeviceType.LIGHTING:
            return _rejected("wrong_device_type", device_id=device_id)
        if state.health != DeviceHealth.ONLINE:
            return _rejected("device_unavailable", health=state.health.value)
        # Brightness, colour temperature and scene are irrelevant shutdown
        # fields in fixed gateway schemas.  Accept harmless placeholders when
        # powering off, just as HvacApp does for its target temperature.
        if power_on and not 0 <= brightness_pct <= 100:
            return _rejected("brightness_out_of_range")
        if power_on and not 2700 <= color_temperature_k <= 6500:
            return _rejected("color_temperature_out_of_range")
        if power_on and brightness_pct == 0:
            return _rejected("active_lighting_requires_brightness")

        state.power_on = power_on
        state.mode = scene if power_on else "off"
        state.level = _level_for_brightness(brightness_pct) if power_on else 0
        state.settings = {
            "brightness_pct": brightness_pct if power_on else 0,
            "color_temperature_k": color_temperature_k if power_on else None,
            "scene": scene if power_on else "off",
        }
        event = self.world.publish_building_event(
            BuildingEventType.DEVICE_STATE_CHANGED,
            source=self.name,
            subject_id=device_id,
            payload={"power_on": power_on, **state.settings},
        )
        return {
            "status": "accepted",
            "device_id": device_id,
            "event_id": event.event_id,
        }


def _level_for_brightness(brightness_pct: int) -> int:
    return 1 if brightness_pct <= 33 else 2 if brightness_pct <= 66 else 3


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **details}
