"""Deterministic reservation service for shared building resources."""

from __future__ import annotations

from typing import Any

from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.room_app import RoomApp
from fairy.apps.building_world.types import (
    BuildingEventType,
    ReservationStatus,
    ResourceReservation,
)
from fairy.tool_utils import OperationType, app_tool, data_tool
from fairy.types import event_registered
from fairy.utils.type_utils import type_check


class ResourceAllocationApp(App):
    def __init__(self, world: BuildingWorldApp, room_app: RoomApp) -> None:
        super().__init__(name="ResourceAllocationApp")
        self.world = world
        self.room_app = room_app

    def reserve_room(self, room_id, owner_id, start_at, end_at):
        """Atomically reserve a room after rechecking current conflicts."""

        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            return {"status": "rejected", "reason": "unknown_room"}
        room_id = canonical_room_id
        conflict = self.room_app.room_conflict(room_id, start_at, end_at)
        if conflict is not None:
            return {"status": "rejected", "reason": "room_conflict", **conflict}
        reservation = ResourceReservation(
            reservation_id=self.world.next_id("reservation"),
            resource_id=room_id,
            owner_id=owner_id,
            start_at=start_at,
            end_at=end_at,
        )
        self.world.reservations[reservation.reservation_id] = reservation
        event = self.world.publish_building_event(
            BuildingEventType.RESOURCE_RESERVED,
            source=self.name,
            subject_id=reservation.reservation_id,
            payload={"room_id": room_id, "owner_id": owner_id},
        )
        return {
            "status": "reserved",
            "reservation": reservation,
            "event_id": event.event_id,
        }

    def release_reservation(
        self, reservation_id: str, *, cancelled: bool = False
    ) -> dict[str, Any]:
        reservation = self.world.reservations.get(reservation_id)
        if reservation is None:
            return {"status": "not_found", "reservation_id": reservation_id}
        if reservation.status != ReservationStatus.ACTIVE:
            return {
                "status": "unchanged",
                "reservation_id": reservation_id,
                "reservation_status": reservation.status.value,
            }
        reservation.status = (
            ReservationStatus.CANCELLED if cancelled else ReservationStatus.RELEASED
        )
        event = self.world.publish_building_event(
            BuildingEventType.RESOURCE_RELEASED,
            source=self.name,
            subject_id=reservation_id,
            payload={"room_id": reservation.resource_id},
        )
        return {
            "status": reservation.status.value,
            "reservation_id": reservation_id,
            "event_id": event.event_id,
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_active_reservations(self, room_id: str) -> dict[str, Any]:
        """Return active reservations for a room without exposing internals."""

        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            return {"error": f"unknown room_id {room_id!r}"}
        room_id = canonical_room_id
        reservations = [
            {
                "reservation_id": item.reservation_id,
                "owner_id": item.owner_id,
                "start_at": item.start_at.isoformat(),
                "end_at": item.end_at.isoformat(),
            }
            for item in self.world.reservations.values()
            if item.resource_id == room_id
            and item.status == ReservationStatus.ACTIVE
        ]
        return {"room_id": room_id, "reservations": reservations}
