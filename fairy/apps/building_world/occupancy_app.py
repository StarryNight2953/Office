"""Person presence and location tools for Building World."""

from __future__ import annotations

from typing import Any

from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.types import BuildingEventType
from fairy.tool_utils import OperationType, app_tool, data_tool
from fairy.types import event_registered
from fairy.utils.type_utils import type_check


class OccupancyApp(App):
    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="OccupancyApp")
        self.world = world

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_person_presence(self, person_id: str) -> dict[str, Any]:
        """Return whether a known person is on campus and their observable zone."""

        person = self.world.people.get(person_id)
        if person is None:
            return {"error": f"unknown person_id {person_id!r}"}
        return {
            "person_id": person.person_id,
            "role": person.role.value,
            "on_campus": person.on_campus,
            "current_zone_id": person.current_zone_id,
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_room_occupancy(self, room_id: str) -> dict[str, Any]:
        """Return the aggregate observable occupancy count for one room."""

        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            return {"error": f"unknown room_id {room_id!r}"}
        room_id = canonical_room_id
        room = self.world.rooms.get(room_id)
        state = self.world.room_states.get(room_id)
        if room is None or state is None:
            return {"error": f"room state unavailable for {room_id!r}"}
        return {
            "room_id": room_id,
            "occupancy_count": state.occupancy_count,
            "capacity": room.capacity,
            "occupancy_fraction": state.occupancy_count / room.capacity,
        }

    def set_person_presence(
        self, person_id: str, on_campus: bool, zone_id: str | None = None
    ) -> dict[str, Any]:
        person = self.world.people.get(person_id)
        if person is None:
            return {"error": f"unknown person_id {person_id!r}"}
        if (
            zone_id is not None
            and zone_id not in self.world.zones
            and zone_id not in self.world.rooms
        ):
            return {"error": f"unknown zone_id {zone_id!r}"}
        person.on_campus = bool(on_campus)
        person.current_zone_id = zone_id if on_campus else None
        event = self.world.publish_building_event(
            BuildingEventType.PERSON_ARRIVED
            if on_campus
            else BuildingEventType.PERSON_LEFT,
            source=self.name,
            subject_id=person_id,
            payload={"zone_id": person.current_zone_id},
        )
        return {"status": "ok", "event_id": event.event_id}
