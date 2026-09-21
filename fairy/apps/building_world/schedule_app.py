"""Deterministic meeting scheduling tools for Building World."""

from __future__ import annotations

from typing import Annotated, Any

from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.datetime_utils import parse_interval
from fairy.apps.building_world.resource_allocation_app import ResourceAllocationApp
from fairy.apps.building_world.room_app import RoomApp
from fairy.apps.building_world.types import (
    BuildingEventType,
    MeetingStatus,
    PersonRole,
    ScheduleEntry,
    time_ranges_overlap,
)
from fairy.tool_utils import OperationType, app_tool, data_tool
from fairy.types import event_registered
from fairy.utils.type_utils import type_check


class ScheduleApp(App):
    """Own meeting lifecycle rules; the LLM cannot bypass these checks."""

    def __init__(
        self,
        world: BuildingWorldApp,
        room_app: RoomApp,
        allocation_app: ResourceAllocationApp,
    ) -> None:
        super().__init__(name="ScheduleApp")
        self.world = world
        self.room_app = room_app
        self.allocation_app = allocation_app

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_person_schedule(
        self,
        person_id: str,
        start_at: Annotated[str, {"format": "date-time"}],
        end_at: Annotated[str, {"format": "date-time"}],
    ) -> dict[str, Any]:
        """Return confirmed meetings involving one person in a time window."""

        if person_id not in self.world.people:
            return {"error": f"unknown person_id {person_id!r}"}
        try:
            start, end = parse_interval(start_at, end_at)
        except ValueError as exc:
            return {"error": str(exc)}
        meetings = [
            _meeting_summary(meeting)
            for meeting in sorted(
                self.world.schedule.values(), key=lambda item: item.start_at
            )
            if meeting.status == MeetingStatus.CONFIRMED
            and (
                meeting.organizer_id == person_id
                or person_id in meeting.participant_ids
            )
            and time_ranges_overlap(start, end, meeting.start_at, meeting.end_at)
        ]
        return {
            "person_id": person_id,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "meetings": meetings,
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_room_schedule(
        self,
        room_id: str,
        start_at: Annotated[str, {"format": "date-time"}],
        end_at: Annotated[str, {"format": "date-time"}],
    ) -> dict[str, Any]:
        """Return confirmed meetings occupying a room in a time window."""

        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            return {"error": f"unknown room_id {room_id!r}"}
        room_id = canonical_room_id
        try:
            start, end = parse_interval(start_at, end_at)
        except ValueError as exc:
            return {"error": str(exc)}
        meetings = [
            _meeting_summary(meeting)
            for meeting in sorted(
                self.world.schedule.values(), key=lambda item: item.start_at
            )
            if meeting.status == MeetingStatus.CONFIRMED
            and meeting.room_id == room_id
            and time_ranges_overlap(start, end, meeting.start_at, meeting.end_at)
        ]
        return {
            "room_id": room_id,
            "start_at": start.isoformat(),
            "end_at": end.isoformat(),
            "meetings": meetings,
        }

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def create_meeting(
        self,
        request_id: str,
        organizer_id: str,
        participant_ids: list[str],
        room_id: str,
        start_at: Annotated[str, {"format": "date-time"}],
        end_at: Annotated[str, {"format": "date-time"}],
        expected_attendees: Annotated[int, {"minimum": 1}],
        required_capabilities: list[str],
        title: str,
    ) -> dict[str, Any]:
        """Validate, reserve and create one confirmed meeting atomically."""

        try:
            start, end = parse_interval(start_at, end_at)
        except ValueError as exc:
            return _rejected("invalid_time", detail=str(exc))
        organizer = self.world.people.get(organizer_id)
        if organizer is None:
            return _rejected("unknown_organizer", organizer_id=organizer_id)
        if organizer.role in {PersonRole.UNKNOWN, PersonRole.VISITOR}:
            return _rejected("organizer_not_authorized", organizer_id=organizer_id)
        unknown_participants = sorted(set(participant_ids) - set(self.world.people))
        if unknown_participants:
            return _rejected(
                "unknown_participants", participant_ids=unknown_participants
            )
        if expected_attendees <= 0:
            return _rejected("invalid_attendee_count")
        if expected_attendees < len(set(participant_ids) | {organizer_id}):
            return _rejected("attendee_count_below_named_people")
        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            return _rejected("unknown_room", room_id=room_id)
        room_id = canonical_room_id
        room = self.world.rooms[room_id]
        if not room.bookable:
            return _rejected("room_not_bookable", room_id=room_id)
        if expected_attendees > room.capacity:
            return _rejected(
                "room_capacity_exceeded",
                room_capacity=room.capacity,
                expected_attendees=expected_attendees,
            )
        missing_capabilities = sorted(
            set(required_capabilities) - set(room.capabilities)
        )
        if missing_capabilities:
            return _rejected(
                "missing_room_capabilities",
                missing_capabilities=missing_capabilities,
            )

        room_conflict = self.room_app.room_conflict(room_id, start, end)
        if room_conflict is not None:
            return _rejected("room_conflict", **room_conflict)
        person_conflicts = self._person_conflicts(
            [organizer_id, *participant_ids], start, end
        )
        if person_conflicts:
            return _rejected("person_conflict", conflicts=person_conflicts)

        reservation_result = self.allocation_app.reserve_room(
            room_id=room_id,
            owner_id=request_id,
            start_at=start,
            end_at=end,
        )
        if reservation_result["status"] != "reserved":
            return _rejected(
                str(reservation_result.get("reason", "reservation_failed")),
                conflicting_id=reservation_result.get("conflicting_id"),
            )
        reservation = reservation_result["reservation"]
        meeting = ScheduleEntry(
            meeting_id=self.world.next_id("meeting"),
            room_id=room_id,
            organizer_id=organizer_id,
            participant_ids=tuple(dict.fromkeys(participant_ids)),
            start_at=start,
            end_at=end,
            expected_attendees=expected_attendees,
            status=MeetingStatus.CONFIRMED,
            title=title,
            required_capabilities=tuple(sorted(set(required_capabilities))),
            reservation_id=reservation.reservation_id,
        )
        try:
            self.world.add_schedule_entry(meeting)
        except Exception:
            self.allocation_app.release_reservation(
                reservation.reservation_id, cancelled=True
            )
            raise
        event = self.world.publish_building_event(
            BuildingEventType.SCHEDULE_CREATED,
            source=self.name,
            subject_id=meeting.meeting_id,
            parent_event_id=reservation_result["event_id"],
            payload={
                "request_id": request_id,
                "room_id": room_id,
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
            },
        )
        return {
            "status": "confirmed",
            "meeting": _meeting_summary(meeting),
            "reservation_id": reservation.reservation_id,
            "event_id": event.event_id,
        }

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def cancel_meeting(
        self, meeting_id: str, requester_id: str, reason: str
    ) -> dict[str, Any]:
        """Cancel a meeting and release its room reservation."""

        meeting = self.world.schedule.get(meeting_id)
        if meeting is None:
            return _rejected("unknown_meeting", meeting_id=meeting_id)
        if meeting.status != MeetingStatus.CONFIRMED:
            return {
                "status": "unchanged",
                "meeting_id": meeting_id,
                "meeting_status": meeting.status.value,
            }
        requester = self.world.people.get(requester_id)
        if requester is None:
            return _rejected("unknown_requester", requester_id=requester_id)
        if requester_id != meeting.organizer_id and requester.role not in {
            PersonRole.STAFF,
            PersonRole.LEADER,
        }:
            return _rejected("requester_not_authorized", requester_id=requester_id)
        meeting.status = MeetingStatus.CANCELLED
        release_result = None
        if meeting.reservation_id is not None:
            release_result = self.allocation_app.release_reservation(
                meeting.reservation_id, cancelled=True
            )
        event = self.world.publish_building_event(
            BuildingEventType.SCHEDULE_CANCELLED,
            source=self.name,
            subject_id=meeting_id,
            payload={"requester_id": requester_id, "reason": reason},
        )
        return {
            "status": "cancelled",
            "meeting_id": meeting_id,
            "release": release_result,
            "event_id": event.event_id,
        }

    def _person_conflicts(self, person_ids, start_at, end_at) -> list[dict[str, str]]:
        conflicts: list[dict[str, str]] = []
        for person_id in sorted(set(person_ids)):
            for meeting in sorted(
                self.world.schedule.values(), key=lambda item: item.meeting_id
            ):
                if meeting.status != MeetingStatus.CONFIRMED:
                    continue
                involved = (
                    meeting.organizer_id == person_id
                    or person_id in meeting.participant_ids
                )
                if involved and time_ranges_overlap(
                    start_at, end_at, meeting.start_at, meeting.end_at
                ):
                    conflicts.append(
                        {
                            "person_id": person_id,
                            "meeting_id": meeting.meeting_id,
                        }
                    )
                    break
        return conflicts


def _meeting_summary(meeting: ScheduleEntry) -> dict[str, Any]:
    return {
        "meeting_id": meeting.meeting_id,
        "title": meeting.title,
        "room_id": meeting.room_id,
        "organizer_id": meeting.organizer_id,
        "participant_ids": list(meeting.participant_ids),
        "start_at": meeting.start_at.isoformat(),
        "end_at": meeting.end_at.isoformat(),
        "expected_attendees": meeting.expected_attendees,
        "required_capabilities": list(meeting.required_capabilities),
        "status": meeting.status.value,
    }


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **details}
