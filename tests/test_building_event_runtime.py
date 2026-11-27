from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartoffice.apps.building_world import (
    BuildingEvent,
    BuildingEventType,
    DeviceHealth,
)
from smartoffice.apps.building_world.room_loader import load_room_configuration
from smartoffice.apps.building_world.runtime import (
    BuildingWorldRuntime,
    constant_outdoor_provider,
)
from smartoffice.controllers.building import (
    BuildingEventQueue,
    BuildingTriggerPolicy,
)
from smartoffice.physics.building import OutdoorConditions


CONFIG_PATH = (
    Path(__file__).parents[1] / "smartoffice" / "configs" / "rooms" / "k1324.yaml"
)
START_AT = datetime(2026, 9, 10, 5, 0, tzinfo=timezone.utc)


def _runtime() -> BuildingWorldRuntime:
    return BuildingWorldRuntime.from_room_configuration(
        load_room_configuration(CONFIG_PATH),
        start_at=START_AT,
        outdoor_provider=constant_outdoor_provider(
            OutdoorConditions(
                air_temperature_c=32.0,
                relative_humidity_pct=70.0,
                co2_ppm=420.0,
                pm25_ug_m3=45.0,
            )
        ),
    )


def test_event_queue_has_stable_order_cancellation_and_snapshot() -> None:
    queue = BuildingEventQueue()
    execute_at = START_AT + timedelta(minutes=10)
    first = queue.schedule(
        execute_at=execute_at,
        event_type=BuildingEventType.PERSON_ARRIVED,
        source="scenario",
        subject_id="leader-01",
    )
    cancelled = queue.schedule(
        execute_at=execute_at,
        event_type=BuildingEventType.PERSON_LEFT,
        source="scenario",
        subject_id="visitor-01",
    )
    third = queue.schedule(
        execute_at=execute_at,
        event_type=BuildingEventType.MANUAL_OVERRIDE,
        source="operator",
        subject_id="k1324_hvac_01",
    )
    assert queue.cancel(cancelled.scheduled_id) is True
    assert queue.cancel(cancelled.scheduled_id) is False

    restored = BuildingEventQueue()
    restored.restore(queue.snapshot())

    assert restored.next_time() == execute_at
    assert restored.pop_due(execute_at) == (first, third)
    assert restored.next_time() is None


def test_trigger_policy_suppresses_telemetry_and_surfaces_adaptation() -> None:
    policy = BuildingTriggerPolicy()
    sensor = BuildingEvent(
        event_id="event-0001",
        event_type=BuildingEventType.SENSOR_UPDATED,
        occurred_at=START_AT,
        source="sensor-hub",
        subject_id="k1324",
    )
    failure = BuildingEvent(
        event_id="event-0002",
        event_type=BuildingEventType.DEVICE_FAILED,
        occurred_at=START_AT,
        source="monitor",
        subject_id="k1324_hvac_01",
    )
    ordinary_arrival = BuildingEvent(
        event_id="event-0003",
        event_type=BuildingEventType.PERSON_ARRIVED,
        occurred_at=START_AT,
        source="access",
        subject_id="student-01",
    )
    leader_arrival = BuildingEvent(
        event_id="event-0004",
        event_type=BuildingEventType.PERSON_ARRIVED,
        occurred_at=START_AT,
        source="access",
        subject_id="leader-01",
        payload={"is_key_person": True},
    )

    assert policy.decide(sensor).should_wake_agent is False
    assert policy.decide(failure).should_wake_agent is True
    assert policy.decide(ordinary_arrival).should_wake_agent is False
    assert policy.decide(leader_arrival).should_wake_agent is True


def test_runtime_stops_at_event_boundary_applies_handler_and_records_decisions() -> None:
    runtime = _runtime()
    failed_at = START_AT + timedelta(minutes=7)

    def fail_device(scheduled, world) -> None:
        state = world.device_states[scheduled.subject_id]
        state.health = DeviceHealth.FAILED
        state.power_on = False

    runtime.register_event_handler(BuildingEventType.DEVICE_FAILED, fail_device)
    runtime.schedule_event(
        execute_at=failed_at,
        event_type=BuildingEventType.DEVICE_FAILED,
        source="simulated-monitor",
        subject_id="k1324_hvac_01",
        payload={"fault": "compressor_trip"},
    )

    result = runtime.advance_to(START_AT + timedelta(minutes=10))

    # The normal five-minute tick is split at minute seven, exactly where the
    # external fault becomes observable.
    assert [step.at_time for step in result.steps] == [
        START_AT + timedelta(minutes=5),
        failed_at,
        START_AT + timedelta(minutes=10),
    ]
    assert runtime.world.device_states["k1324_hvac_01"].health == (
        DeviceHealth.FAILED
    )
    failure_events = [
        event
        for event in result.processed_events
        if event.event_type == BuildingEventType.DEVICE_FAILED
    ]
    assert len(failure_events) == 1
    assert failure_events[0].occurred_at == failed_at
    assert any(
        decision.event_id == failure_events[0].event_id
        and decision.should_wake_agent
        for decision in result.trigger_decisions
    )
    sensor_decisions = [
        decision
        for decision in result.trigger_decisions
        if decision.event_type == BuildingEventType.SENSOR_UPDATED
    ]
    assert sensor_decisions
    assert all(not decision.should_wake_agent for decision in sensor_decisions)


def test_runtime_checkpoint_restores_pending_event_and_classification_cursor() -> None:
    runtime = _runtime()
    execute_at = START_AT + timedelta(minutes=8)
    runtime.schedule_event(
        execute_at=execute_at,
        event_type=BuildingEventType.MEETING_ENDED_EARLY,
        source="schedule-service",
        subject_id="meeting-001",
    )
    runtime.advance_by(3 * 60)
    checkpoint = runtime.snapshot()

    first_result = runtime.advance_to(START_AT + timedelta(minutes=10))
    first_event_ids = [
        event.event_id
        for event in first_result.processed_events
        if event.event_type == BuildingEventType.MEETING_ENDED_EARLY
    ]
    first_decisions = [
        decision.should_wake_agent
        for decision in first_result.trigger_decisions
        if decision.event_type == BuildingEventType.MEETING_ENDED_EARLY
    ]

    runtime.restore(checkpoint)
    replay_result = runtime.advance_to(START_AT + timedelta(minutes=10))

    assert [
        event.event_id
        for event in replay_result.processed_events
        if event.event_type == BuildingEventType.MEETING_ENDED_EARLY
    ] == first_event_ids
    assert [
        decision.should_wake_agent
        for decision in replay_result.trigger_decisions
        if decision.event_type == BuildingEventType.MEETING_ENDED_EARLY
    ] == first_decisions == [True]
