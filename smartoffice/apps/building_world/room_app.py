"""Read-only room discovery and availability tools."""

from __future__ import annotations

from typing import Annotated, Any

from smartoffice.apps.app import App
from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.datetime_utils import parse_interval
from smartoffice.apps.building_world.types import (
    MeetingStatus,
    ReservationStatus,
    time_ranges_overlap,
)
from smartoffice.tool_utils import OperationType, app_tool, data_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check


class RoomApp(App):
    def __init__(self, world: BuildingWorldApp) -> None:
        super().__init__(name="RoomApp")
        self.world = world

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def find_available_rooms(
        self,
        start_at: Annotated[str, {"format": "date-time"}],
        end_at: Annotated[str, {"format": "date-time"}],
        min_capacity: int,
        required_capabilities: list[str],
    ) -> dict[str, Any]:
        """Find bookable rooms satisfying capacity, capability and time constraints."""

        try:
            start, end = parse_interval(start_at, end_at)
        except ValueError as exc:
            return {"error": str(exc)}
        if min_capacity <= 0:
            return {"error": "min_capacity must be positive"}
        required = set(required_capabilities)
        rooms = []
        for room in sorted(self.world.rooms.values(), key=lambda item: item.room_id):
            if not room.bookable:
                continue
            if room.capacity < min_capacity:
                continue
            if not required.issubset(room.capabilities):
                continue
            conflict = self.room_conflict(room.room_id, start, end)
            if conflict is None:
                rooms.append(
                    {
                        "room_id": room.room_id,
                        "name": room.name,
                        "capacity": room.capacity,
                        "capabilities": sorted(room.capabilities),
                        "room_type": room.room_type,
                        "bookable": room.bookable,
                    }
                )
        return {
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "available_rooms": rooms,
        }

    def room_conflict(self, room_id, start_at, end_at) -> dict[str, str] | None:
        """Return the first deterministic meeting/reservation conflict."""

        for meeting in sorted(
            self.world.schedule.values(), key=lambda item: item.meeting_id
        ):
            if meeting.status != MeetingStatus.CONFIRMED:
                continue
            if meeting.room_id == room_id and time_ranges_overlap(
                start_at, end_at, meeting.start_at, meeting.end_at
            ):
                return {
                    "type": "meeting",
                    "conflicting_id": meeting.meeting_id,
                }
        for reservation in sorted(
            self.world.reservations.values(), key=lambda item: item.reservation_id
        ):
            if reservation.status != ReservationStatus.ACTIVE:
                continue
            if reservation.resource_id == room_id and time_ranges_overlap(
                start_at, end_at, reservation.start_at, reservation.end_at
            ):
                return {
                    "type": "reservation",
                    "conflicting_id": reservation.reservation_id,
                }
        return None
