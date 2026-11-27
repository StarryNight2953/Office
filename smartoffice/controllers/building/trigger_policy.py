"""Deterministic policy deciding whether a building event needs an Agent."""

from __future__ import annotations

from dataclasses import dataclass

from smartoffice.apps.building_world.types import BuildingEvent, BuildingEventType


@dataclass(frozen=True)
class TriggerDecision:
    """Auditable wake/suppress decision for one observed domain event."""

    event_id: str
    event_type: BuildingEventType
    should_wake_agent: bool
    reason: str


class BuildingTriggerPolicy:
    """Suppress routine telemetry while surfacing plan-invalidating changes."""

    _ALWAYS_WAKE = frozenset(
        {
            BuildingEventType.SCHEDULE_CHANGED,
            BuildingEventType.SCHEDULE_CANCELLED,
            BuildingEventType.DEVICE_FAILED,
            BuildingEventType.MANUAL_OVERRIDE,
            BuildingEventType.COMFORT_THRESHOLD_VIOLATED,
            BuildingEventType.AIR_QUALITY_THRESHOLD_VIOLATED,
            BuildingEventType.MEETING_PREPARATION_DUE,
            BuildingEventType.MEETING_ENDED,
            BuildingEventType.ENVIRONMENT_CHECK_DUE,
            BuildingEventType.MEETING_DELAYED,
            BuildingEventType.MEETING_ENDED_EARLY,
            # Whole-room occupancy changes alter thermal and air-quality loads
            # and therefore invalidate the previous control plan.
            BuildingEventType.OCCUPANCY_CHANGED,
            BuildingEventType.PRINT_REQUEST_ADDED,
        }
    )
    _ROUTINE = frozenset(
        {
            BuildingEventType.SENSOR_UPDATED,
            BuildingEventType.DEVICE_STATE_CHANGED,
            BuildingEventType.RESOURCE_RESERVED,
            BuildingEventType.RESOURCE_RELEASED,
            BuildingEventType.THRESHOLD_RECOVERED,
            BuildingEventType.MEETING_STARTED,
        }
    )

    def decide(self, event: BuildingEvent) -> TriggerDecision:
        """Classify one event without mutating runtime or world state."""

        explicit = event.payload.get("wake_agent")
        if isinstance(explicit, bool):
            return TriggerDecision(
                event.event_id,
                event.event_type,
                explicit,
                "explicit_event_override",
            )
        if event.event_type in self._ALWAYS_WAKE:
            return TriggerDecision(
                event.event_id,
                event.event_type,
                True,
                "plan_or_environment_may_require_adaptation",
            )
        if event.event_type == BuildingEventType.SCHEDULE_CREATED:
            requires_preparation = bool(
                event.payload.get("requires_preparation", False)
            )
            return TriggerDecision(
                event.event_id,
                event.event_type,
                requires_preparation,
                (
                    "new_schedule_requires_preparation"
                    if requires_preparation
                    else "schedule_recorded_without_immediate_preparation"
                ),
            )
        if event.event_type in {
            BuildingEventType.PERSON_ARRIVED,
            BuildingEventType.PERSON_LEFT,
        }:
            key_person = bool(event.payload.get("is_key_person", False))
            return TriggerDecision(
                event.event_id,
                event.event_type,
                key_person,
                (
                    "key_person_presence_changed"
                    if key_person
                    else "routine_presence_change"
                ),
            )
        if event.event_type in self._ROUTINE:
            return TriggerDecision(
                event.event_id,
                event.event_type,
                False,
                "routine_event_handled_without_agent",
            )
        return TriggerDecision(
            event.event_id,
            event.event_type,
            False,
            "no_wakeup_rule",
        )
