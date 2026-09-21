"""Presentation and audio controls used during normal meeting preparation."""

from __future__ import annotations

from typing import Annotated, Any

from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.types import BuildingEventType, DeviceHealth, DeviceType
from fairy.tool_utils import OperationType, app_tool, data_tool
from fairy.types import event_registered
from fairy.utils.type_utils import type_check


class MeetingEquipmentApp(App):
    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="MeetingEquipmentApp")
        self.world = world

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_projector(
        self, device_id: str, power_on: bool, input_source: str
    ) -> dict[str, Any]:
        return self._set_device(
            device_id,
            DeviceType.PROJECTOR,
            power_on,
            {"input_source": input_source},
        )

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_audio_system(
        self,
        device_id: str,
        power_on: bool,
        volume_pct: Annotated[int, {"minimum": 0, "maximum": 100}],
        microphone_enabled: bool,
    ) -> dict[str, Any]:
        if power_on and not 0 <= volume_pct <= 100:
            return _rejected("volume_out_of_range")
        return self._set_device(
            device_id,
            DeviceType.AUDIO,
            power_on,
            {
                "volume_pct": volume_pct if power_on else 0,
                "microphone_enabled": microphone_enabled if power_on else False,
            },
        )

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_meeting_equipment_readiness(self, room_id: str) -> dict[str, Any]:
        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            return {"error": f"unknown room_id {room_id!r}"}
        room_id = canonical_room_id
        rows = []
        for device_id, spec in self.world.devices.items():
            if spec.room_id != room_id or spec.device_type not in {
                DeviceType.PROJECTOR,
                DeviceType.AUDIO,
            }:
                continue
            state = self.world.device_states[device_id]
            rows.append(
                {
                    "device_id": device_id,
                    "device_type": spec.device_type.value,
                    "ready": state.health == DeviceHealth.ONLINE and state.power_on,
                    "settings": dict(state.settings),
                }
            )
        return {
            "room_id": room_id,
            "ready": bool(rows) and all(row["ready"] for row in rows),
            "devices": rows,
        }

    def _set_device(
        self,
        device_id: str,
        expected_type: DeviceType,
        power_on: bool,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None:
            return _rejected("unknown_device", device_id=device_id)
        if spec.device_type != expected_type:
            return _rejected("wrong_device_type", device_id=device_id)
        if state.health != DeviceHealth.ONLINE:
            return _rejected("device_unavailable", health=state.health.value)

        state.power_on = power_on
        state.mode = "ready" if power_on else "off"
        state.level = 1 if power_on else 0
        state.settings = (
            settings
            if power_on
            else {
                key: False
                if isinstance(value, bool)
                else 0
                if isinstance(value, int)
                else "off"
                for key, value in settings.items()
            }
        )
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


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **details}
