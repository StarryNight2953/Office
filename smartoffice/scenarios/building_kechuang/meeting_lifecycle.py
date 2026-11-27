"""Reusable normal meeting phases for Kechuang room scenarios."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.runtime import BuildingWorldRuntime
from smartoffice.apps.building_world.types import (
    BuildingEvent,
    BuildingEventType,
    MeetingStatus,
    ReservationStatus,
)
from smartoffice.controllers.building import ScheduledBuildingEvent


@dataclass(frozen=True)
class MeetingPhase:
    """Declarative future phase used by normal lifecycle scenarios."""

    phase_id: str
    execute_at: datetime
    event_type: BuildingEventType
    subject_id: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.phase_id or not self.subject_id:
            raise ValueError("meeting phase identity cannot be empty")
        if self.execute_at.tzinfo is None:
            raise ValueError("meeting phase time must be timezone-aware")


def install_normal_lifecycle(
    runtime: BuildingWorldRuntime,
    phases: Sequence[MeetingPhase],
) -> tuple[ScheduledBuildingEvent, ...]:
    """Register normal state handlers and enqueue a declarative phase list."""

    runtime.register_event_handler(
        BuildingEventType.OCCUPANCY_CHANGED, _apply_occupancy
    )
    runtime.register_event_handler(
        BuildingEventType.MEETING_STARTED, _apply_meeting_started
    )
    runtime.register_event_handler(
        BuildingEventType.MEETING_ENDED, _apply_meeting_ended
    )
    scheduled: list[ScheduledBuildingEvent] = []
    for phase in phases:
        scheduled.append(
            runtime.schedule_event(
                scheduled_id=phase.phase_id,
                execute_at=phase.execute_at,
                event_type=phase.event_type,
                source="MeetingLifecycle",
                subject_id=phase.subject_id,
                payload=phase.payload,
            )
        )
    return tuple(scheduled)


def event_types_in_order(events: Sequence[BuildingEvent]) -> list[BuildingEventType]:
    """Return lifecycle event types in their actual publication order."""

    lifecycle_types = {
        BuildingEventType.MEETING_PREPARATION_DUE,
        BuildingEventType.MEETING_STARTED,
        BuildingEventType.MEETING_ENDED,
        BuildingEventType.ENVIRONMENT_CHECK_DUE,
        BuildingEventType.OCCUPANCY_CHANGED,
    }
    return [event.event_type for event in events if event.event_type in lifecycle_types]


def _apply_occupancy(
    scheduled: ScheduledBuildingEvent,
    world: BuildingWorldApp,
) -> None:
    room_id = str(scheduled.payload.get("room_id", scheduled.subject_id))
    count = int(scheduled.payload.get("occupancy_count", 0))
    if room_id not in world.room_states:
        raise ValueError(f"occupancy phase references unknown room {room_id!r}")
    if count < 0 or count > world.rooms[room_id].capacity:
        raise ValueError(f"invalid occupancy_count {count} for room {room_id!r}")
    world.room_states[room_id].occupancy_count = count


def _apply_meeting_started(
    scheduled: ScheduledBuildingEvent,
    world: BuildingWorldApp,
) -> None:
    meeting = world.schedule.get(scheduled.subject_id)
    if meeting is None:
        raise ValueError(
            f"meeting-start phase references unknown meeting {scheduled.subject_id!r}"
        )
    count = int(scheduled.payload.get("occupancy_count", meeting.expected_attendees))
    if count < 0 or count > world.rooms[meeting.room_id].capacity:
        raise ValueError("meeting occupancy is outside room capacity")
    world.room_states[meeting.room_id].occupancy_count = count


def _apply_meeting_ended(
    scheduled: ScheduledBuildingEvent,
    world: BuildingWorldApp,
) -> None:
    meeting = world.schedule.get(scheduled.subject_id)
    if meeting is None:
        raise ValueError(
            f"meeting-end phase references unknown meeting {scheduled.subject_id!r}"
        )
    meeting.status = MeetingStatus.COMPLETED
    world.room_states[meeting.room_id].occupancy_count = 0
    if meeting.reservation_id is None:
        return
    reservation = world.reservations.get(meeting.reservation_id)
    if reservation is None or reservation.status != ReservationStatus.ACTIVE:
        return
    reservation.status = ReservationStatus.RELEASED
    world.publish_building_event(
        BuildingEventType.RESOURCE_RELEASED,
        source="MeetingLifecycle",
        subject_id=reservation.reservation_id,
        occurred_at=scheduled.execute_at,
        payload={"room_id": meeting.room_id, "meeting_id": meeting.meeting_id},
    )
