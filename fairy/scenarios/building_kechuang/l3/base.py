"""Shared lifecycle, reference workflow and validator for all twenty L3s."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fairy.apps.agent_user_interface import AgentUserInterface
from fairy.apps.building_world import (
    AirDeviceApp,
    BuildingEventType,
    BuildingOperationsApp,
    BuildingSensorApp,
    BuildingWorldApp,
    HvacApp,
    LightingApp,
    MeetingEquipmentApp,
    MeetingStatus,
    PrintingApp,
    ReservationStatus,
    ResourceReservation,
    ScheduleEntry,
    VentilationApp,
)
from fairy.apps.building_world.runtime import OutdoorProvider
from fairy.apps.system import SystemApp
from fairy.controllers.building import ScheduledBuildingEvent
from fairy.scenarios.building_kechuang.base import (
    KechuangBuildingScenario,
    collect_event_graph,
)
from fairy.scenarios.building_kechuang.l3.specs import (
    ROOM_CAPACITIES,
    BuildingL3Spec,
)
from fairy.scenarios.building_kechuang.meeting_lifecycle import (
    MeetingPhase,
    install_normal_lifecycle,
)
from fairy.scenarios.validation_result import ScenarioValidationResult
from fairy.types import EventRegisterer


@dataclass(frozen=True)
class WorkflowCheckpoint:
    """One instant where world events are reconciled before control decisions."""

    minute: int
    labels: tuple[str, ...]


@dataclass(frozen=True)
class RoomControlPolicy:
    """Reference control state derived from occupancy, weather and constraints."""

    hvac_mode: str
    hvac_level: int
    ventilation_level: int
    humidifier_level: int
    purifier_level: int
    scene: str


class SpecDrivenBuildingL3Scenario(KechuangBuildingScenario):
    """Execute one declarative full-day scenario through a common protocol.

    Subclasses provide only ``spec``. This class owns the invariant workflow:
    observe, reconcile, act, wait, verify, close and audit. Differences in
    weather, occupancy, meetings, interactions and constraints remain data.
    """

    spec: BuildingL3Spec

    def __post_init__(self) -> None:
        super().__post_init__()
        self.spec.validate()
        self.start_time = self.spec.start_at.timestamp()
        self.duration = float(self.spec.duration_minutes * 60)
        self.scenario_input = self._build_briefing()

    def outdoor_provider(self) -> OutdoorProvider:
        return self.spec.outdoor.provider(self.spec.start_at)

    def configure_initial_runtime(self, runtime) -> None:
        """Install season-specific truth before the first sensor observation."""

        initial_states = runtime.physics.engine.get_state()
        for state in initial_states.values():
            state.air_temperature_c = self.spec.initial_indoor_temperature_c
            state.relative_humidity_pct = self.spec.initial_indoor_humidity_pct
            state.co2_ppm = 500.0
            state.pm25_ug_m3 = 20.0
        runtime.physics.engine.load_state(initial_states)

    def initiate_scenario(self) -> None:
        super().initiate_scenario()
        world = self.get_typed_app(BuildingWorldApp)
        printing = self.get_typed_app(PrintingApp)
        operations = BuildingOperationsApp(
            world=world,
            runtime=self.building_runtime,
            printing=printing,
            system=self.get_typed_app(SystemApp),
            operational_end_at=self.spec.end_at,
            target_temperature_c=self.spec.target_temperature_c,
            max_co2_ppm=self.spec.evaluation.max_peak_co2_ppm,
            max_pm25_ug_m3=self.spec.evaluation.max_peak_pm25_ug_m3,
            power_limit_w=self.spec.power_limit_w,
            max_unoccupied_energy_kwh=self.spec.evaluation.max_unoccupied_energy_kwh,
            preparation_lead_minutes=self.spec.preparation_lead_minutes,
        )
        self.get_typed_app(SystemApp).register_time_advance_precondition(
            operations.time_advance_precondition
        )
        self.apps.append(operations)
        operations.bind_device_tools(self.apps)
        lifecycle: list[MeetingPhase] = []

        for batch_index, batch in enumerate(self.spec.print_batches, start=1):
            request_id = self._print_request_id(batch_index)
            request_payload = self._print_request_payload(batch_index, batch)
            if batch.reveal_minute == 0:
                printing.add_print_request(**request_payload)
            else:
                lifecycle.append(
                    MeetingPhase(
                        f"print-request-{self.spec.number:02d}-{batch_index:02d}",
                        self.spec.start_at
                        + timedelta(minutes=batch.reveal_minute),
                        BuildingEventType.PRINT_REQUEST_ADDED,
                        request_id,
                        {
                            "wake_agent": True,
                            **{
                                key: (
                                    value.isoformat()
                                    if isinstance(value, datetime)
                                    else value
                                )
                                for key, value in request_payload.items()
                            },
                        },
                    )
                )

        # Explicit occupancy phases make planned human activity an exogenous
        # world process. The Agent can observe and respond, but cannot invent it.
        for phase in self.spec.phases:
            for room_id, count in phase.counts:
                lifecycle.append(
                    MeetingPhase(
                        f"{self.spec.number:02d}-{phase.minute:03d}-{room_id}",
                        self.spec.start_at + timedelta(minutes=phase.minute),
                        BuildingEventType.OCCUPANCY_CHANGED,
                        room_id,
                        {
                            "room_id": room_id,
                            "occupancy_count": count,
                            "episode": phase.episode,
                        },
                    )
                )

        for meeting in self.spec.meetings:
            reservation_id = f"{meeting.activity_id}-reservation"
            start = self.spec.start_at + timedelta(minutes=meeting.start_minute)
            end = self.spec.start_at + timedelta(minutes=meeting.end_minute)
            interaction_payload = self._interaction_payload_for(meeting.activity_id)
            initial_room_id = str(interaction_payload.get("from_room", meeting.room_id))
            initial_start = self.spec.start_at + timedelta(
                minutes=int(
                    interaction_payload.get("old_start_minute", meeting.start_minute)
                )
            )
            initial_end = self.spec.start_at + timedelta(
                minutes=int(
                    interaction_payload.get("original_end_minute", meeting.end_minute)
                )
            )
            initial_attendees = int(
                interaction_payload.get("old_attendees", meeting.attendees)
            )
            initial_capabilities = meeting.capabilities
            if interaction_payload.get("added_audio"):
                initial_capabilities = tuple(
                    item
                    for item in meeting.capabilities
                    if item not in {"audio", "microphone"}
                )
            world.reservations[reservation_id] = ResourceReservation(
                reservation_id=reservation_id,
                resource_id=initial_room_id,
                owner_id=meeting.activity_id,
                start_at=initial_start,
                end_at=initial_end,
            )
            world.add_schedule_entry(
                ScheduleEntry(
                    meeting_id=meeting.activity_id,
                    room_id=initial_room_id,
                    organizer_id="staff-01",
                    participant_ids=("professor-01",),
                    start_at=initial_start,
                    end_at=initial_end,
                    expected_attendees=initial_attendees,
                    title=(
                        f"Scheduled activity {meeting.activity_id}"
                        if interaction_payload
                        else meeting.title
                    ),
                    required_capabilities=initial_capabilities,
                    reservation_id=reservation_id,
                )
            )
            lifecycle.extend(
                (
                    MeetingPhase(
                        f"{meeting.activity_id}-prepare",
                        start
                        - timedelta(minutes=self.spec.preparation_lead_minutes),
                        BuildingEventType.MEETING_PREPARATION_DUE,
                        meeting.activity_id,
                        {"room_id": meeting.room_id},
                    ),
                    MeetingPhase(
                        f"{meeting.activity_id}-start",
                        start,
                        BuildingEventType.MEETING_STARTED,
                        meeting.activity_id,
                        {"occupancy_count": meeting.attendees},
                    ),
                    MeetingPhase(
                        f"{meeting.activity_id}-end",
                        end,
                        BuildingEventType.MEETING_ENDED,
                        meeting.activity_id,
                    ),
                )
            )

        if self.spec.interaction is not None:
            interaction = self.spec.interaction
            target_meeting = world.schedule.get(interaction.subject_id)
            target_payload = {}
            if target_meeting is not None:
                target_spec = next(
                    item
                    for item in self.spec.meetings
                    if item.activity_id == interaction.subject_id
                )
                target_payload = {
                    "room_id": target_spec.room_id,
                    "attendees": target_spec.attendees,
                    "start_at": (
                        self.spec.start_at + timedelta(minutes=target_spec.start_minute)
                    ).isoformat(),
                    "end_at": (
                        self.spec.start_at + timedelta(minutes=target_spec.end_minute)
                    ).isoformat(),
                    "required_capabilities": list(target_spec.capabilities),
                }
            lifecycle.append(
                MeetingPhase(
                    f"interaction-{self.spec.number:02d}",
                    self.spec.start_at + timedelta(minutes=interaction.minute),
                    interaction.event_type,
                    interaction.subject_id,
                    {
                        "description": interaction.description,
                        "wake_agent": True,
                        **dict(interaction.payload),
                        **target_payload,
                    },
                )
            )
            self.building_runtime.register_event_handler(
                interaction.event_type, _apply_schedule_interaction
            )
        self.building_runtime.register_event_handler(
            BuildingEventType.PRINT_REQUEST_ADDED,
            lambda scheduled, _world: _apply_print_request(scheduled, printing),
        )
        install_normal_lifecycle(
            self.building_runtime,
            tuple(
                sorted(
                    lifecycle,
                    key=lambda item: (
                        item.execute_at,
                        _same_time_event_priority(item.event_type),
                    ),
                )
            ),
        )

    def _interaction_payload_for(self, activity_id: str) -> dict[str, object]:
        interaction = self.spec.interaction
        if interaction is None or interaction.subject_id != activity_id:
            return {}
        return dict(interaction.payload)

    def _print_request_id(self, batch_index: int) -> str:
        return f"print-request-{self.spec.number:02d}-{batch_index:02d}"

    def _print_request_payload(self, batch_index: int, batch) -> dict[str, object]:
        return {
            "request_id": self._print_request_id(batch_index),
            "document_name": batch.document_name,
            "copies": batch.copies,
            "pages_per_copy": batch.pages_per_copy,
            "requested_by": "staff-01",
            "submit_at": self.spec.start_at
            + timedelta(minutes=batch.submit_minute),
            "ready_by": self.spec.start_at
            + timedelta(minutes=batch.ready_by_minute),
            "priority": batch.priority,
        }

    def build_events_flow(self) -> None:
        """Build a time-aware observe/act/wait/verify reference workflow."""

        aui = self.get_typed_app(AgentUserInterface)
        world = self.get_typed_app(BuildingWorldApp)
        sensors = self.get_typed_app(BuildingSensorApp)
        system = self.get_typed_app(SystemApp)
        hvac = self.get_typed_app(HvacApp)
        air = self.get_typed_app(AirDeviceApp)
        ventilation = self.get_typed_app(VentilationApp)
        lighting = self.get_typed_app(LightingApp)
        equipment = self.get_typed_app(MeetingEquipmentApp)
        printing = self.get_typed_app(PrintingApp)
        operations = self.get_typed_app(BuildingOperationsApp)

        with EventRegisterer.capture_mode():
            briefing = (
                aui.send_message_to_agent(content=self.scenario_input)
                .with_id("briefing")
                .depends_on(None, delay_seconds=1)
            )
            previous = (
                world.get_building_overview()
                .oracle()
                .with_id("observe_agenda_and_resources")
                .depends_on(briefing, delay_seconds=1)
            )
            previous = (
                sensors.read_all_sensors()
                .oracle()
                .with_id("observe_initial_environment")
                .depends_on(previous, delay_seconds=1)
            )
            previous = (
                operations.get_operational_status()
                .oracle()
                .with_id("observe_operational_horizon")
                .depends_on(previous, delay_seconds=1)
            )
            previous = (
                operations.get_commitment_status()
                .oracle()
                .with_id("prioritize_initial_commitments")
                .depends_on(previous, delay_seconds=1)
            )
            if self.spec.print_batches:
                previous = (
                    printing.get_print_requests(status="pending")
                    .oracle()
                    .with_id("observe_public_print_requirements")
                    .depends_on(previous, delay_seconds=1)
                )

            elapsed = 0
            room_policies: dict[str, RoomControlPolicy] = {}
            boosted_rooms: set[str] = set()
            printer_on = False
            print_job_ids: dict[int, str] = {}
            next_print_job_number = 0
            checkpoints = self._workflow_checkpoints()

            for index, checkpoint in enumerate(checkpoints, start=1):
                delta = checkpoint.minute - elapsed
                if delta > 0:
                    previous = (
                        system.advance_time(minutes=delta, stop_on_event=False)
                        .oracle()
                        .with_id(f"advance_to_checkpoint_{index:02d}")
                        .depends_on(previous, delay_seconds=1)
                    )
                    elapsed = checkpoint.minute
                label = self._checkpoint_label(checkpoint)
                previous = (
                    world.get_recent_building_events(limit=20)
                    .oracle()
                    .with_id(f"reconcile_{index:02d}_{label}")
                    .depends_on(previous, delay_seconds=1)
                )
                if self._is_interaction_checkpoint(checkpoint.minute):
                    previous = (
                        world.get_building_overview()
                        .oracle()
                        .with_id(f"confirm_replanned_agenda_{index:02d}")
                        .depends_on(previous, delay_seconds=1)
                    )
                previous = (
                    sensors.read_all_sensors()
                    .oracle()
                    .with_id(f"observe_before_control_{index:02d}_{label}")
                    .depends_on(previous, delay_seconds=1)
                )
                previous = (
                    operations.get_commitment_status()
                    .oracle()
                    .with_id(f"prioritize_commitments_{index:02d}_{label}")
                    .depends_on(previous, delay_seconds=1)
                )
                previous = (
                    operations.get_environment_control_status()
                    .oracle()
                    .with_id(f"assess_environment_{index:02d}_{label}")
                    .depends_on(previous, delay_seconds=1)
                )
                for meeting in self.spec.meetings:
                    if (
                        meeting.start_minute - self.spec.preparation_lead_minutes
                        == checkpoint.minute
                    ):
                        previous = (
                            operations.get_meeting_readiness(meeting.activity_id)
                            .oracle()
                            .with_id(
                                f"check_readiness_{index:02d}_{meeting.activity_id}"
                            )
                            .depends_on(previous, delay_seconds=1)
                        )

                actions_taken = False
                due_batches = sorted(
                    (
                        (batch_index, batch)
                        for batch_index, batch in enumerate(
                            self.spec.print_batches, start=1
                        )
                        if batch.submit_minute == checkpoint.minute
                    ),
                    key=lambda item: (-item[1].priority, item[0]),
                )
                for batch_index, batch in due_batches:
                    if not printer_on:
                        previous = (
                            printing.set_printer_power("k1315_printer_01", True)
                            .oracle()
                            .with_id(f"power_printer_for_batch_{batch_index:02d}")
                            .depends_on(previous, delay_seconds=1)
                        )
                        printer_on = True
                    previous = (
                        printing.submit_print_job(
                            "k1315_printer_01",
                            batch.document_name,
                            batch.copies,
                            batch.pages_per_copy,
                            "staff-01",
                            batch.priority,
                        )
                        .oracle()
                        .with_id(f"submit_print_batch_{batch_index:02d}")
                        .depends_on(previous, delay_seconds=1)
                    )
                    next_print_job_number += 1
                    print_job_ids[batch_index] = (
                        f"print-job-{next_print_job_number:04d}"
                    )
                    actions_taken = True

                desired_counts = self._service_counts_at(checkpoint.minute)
                desired_rooms = set(desired_counts)
                for room_id in sorted(set(room_policies) - desired_rooms):
                    previous = self._capture_room_shutdown(
                        previous,
                        room_id,
                        f"checkpoint_{index:02d}",
                        hvac,
                        air,
                        ventilation,
                        lighting,
                        equipment,
                    )
                    room_policies.pop(room_id)
                    boosted_rooms.discard(room_id)
                    actions_taken = True
                for room_id in sorted(desired_rooms):
                    policy = self._room_policy(
                        room_id,
                        desired_counts[room_id],
                        checkpoint.minute,
                        label,
                        len(desired_rooms),
                    )
                    if room_policies.get(room_id) == policy:
                        continue
                    previous = self._capture_room_control(
                        previous,
                        room_id,
                        policy,
                        f"checkpoint_{index:02d}",
                        hvac,
                        air,
                        ventilation,
                        lighting,
                        equipment,
                    )
                    room_policies[room_id] = policy
                    actions_taken = True

                if actions_taken and self.spec.power_limit_w is not None:
                    previous = (
                        operations.get_power_budget_status()
                        .oracle()
                        .with_id(f"verify_power_budget_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )

                for batch_index, batch in enumerate(self.spec.print_batches, start=1):
                    if batch.ready_by_minute != checkpoint.minute:
                        continue
                    previous = (
                        printing.get_print_job(print_job_ids[batch_index])
                        .oracle()
                        .with_id(f"verify_print_deadline_{batch_index:02d}")
                        .depends_on(previous, delay_seconds=1)
                    )

                next_minute = (
                    checkpoints[index].minute
                    if index < len(checkpoints)
                    else self.spec.duration_minutes
                )
                available_minutes = max(0, next_minute - elapsed)
                if actions_taken and available_minutes > 0:
                    response_wait = min(
                        self.spec.evaluation.response_wait_minutes,
                        available_minutes,
                    )
                    previous = (
                        system.advance_time(
                            minutes=response_wait, stop_on_event=False
                        )
                        .oracle()
                        .with_id(f"wait_for_response_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )
                    elapsed += response_wait
                    previous = (
                        sensors.read_all_sensors()
                        .oracle()
                        .with_id(f"verify_response_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )
                    previous = (
                        operations.get_environment_control_status()
                        .oracle()
                        .with_id(f"verify_control_status_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )
                # ``observe_before_control`` is already the monitoring sample
                # when no command changed; do not emit an identical second read.

                high_load_rooms = {
                    room_id
                    for room_id, count in desired_counts.items()
                    if count / ROOM_CAPACITIES[room_id] >= 0.75
                    and room_id not in boosted_rooms
                }
                remaining_minutes = max(0, next_minute - elapsed)
                if high_load_rooms and remaining_minutes > 0:
                    for room_id in sorted(high_load_rooms):
                        previous = self._capture_feedback_boost(
                            previous,
                            room_id,
                            f"feedback_{index:02d}",
                            hvac,
                            air,
                            ventilation,
                        )
                        boosted_rooms.add(room_id)
                    stability_wait = min(
                        self.spec.evaluation.stability_wait_minutes,
                        remaining_minutes,
                    )
                    previous = (
                        system.advance_time(
                            minutes=stability_wait, stop_on_event=False
                        )
                        .oracle()
                        .with_id(f"wait_for_stability_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )
                    elapsed += stability_wait
                    previous = (
                        sensors.read_all_sensors()
                        .oracle()
                        .with_id(f"verify_stability_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )
                    previous = (
                        operations.get_environment_control_status()
                        .oracle()
                        .with_id(f"confirm_stability_{index:02d}_{label}")
                        .depends_on(previous, delay_seconds=1)
                    )

            if printer_on:
                previous = (
                    printing.set_printer_power("k1315_printer_01", False)
                    .oracle()
                    .with_id("shutdown_shared_printer")
                    .depends_on(previous, delay_seconds=1)
                )
            previous = (
                sensors.read_all_sensors()
                .oracle()
                .with_id("verify_final_environment")
                .depends_on(previous, delay_seconds=1)
            )
            previous = (
                operations.get_operational_status()
                .oracle()
                .with_id("verify_completion_guard_clear")
                .depends_on(previous, delay_seconds=1)
            )
            report = (
                aui.send_message_to_user(
                    content=f"{self.spec.title}已完成；预约、环境设备和共享资源已审计。"
                )
                .oracle()
                .with_id("report_l3_complete")
                .depends_on(previous, delay_seconds=1)
            )
            self.events = collect_event_graph(briefing)
            assert any(event is report for event in self.events)

    def _workflow_checkpoints(self) -> tuple[WorkflowCheckpoint, ...]:
        labels: dict[int, list[str]] = {}

        def add(minute: int, label: str) -> None:
            if 0 <= minute <= self.spec.duration_minutes:
                labels.setdefault(minute, []).append(label)

        for phase in self.spec.phases:
            add(phase.minute, phase.episode)
        # Prepare each room before its first planned occupancy rather than
        # waiting for people to arrive in an untreated room.
        for room_id in self.spec.rooms:
            positive_minutes = [
                phase.minute
                for phase in self.spec.phases
                if phase.occupancy.get(room_id, 0) > 0
            ]
            if positive_minutes:
                add(
                    max(
                        0,
                        min(positive_minutes) - self.spec.preparation_lead_minutes,
                    ),
                    f"precondition_{room_id}",
                )
        # A full-day L3 must continue monitoring between named business
        # transitions. Hourly checkpoints provide bounded feedback cadence.
        for minute in range(60, self.spec.duration_minutes, 60):
            add(minute, "routine_monitor")
        for meeting in self.spec.meetings:
            add(
                meeting.start_minute - self.spec.preparation_lead_minutes,
                f"prepare_{meeting.activity_id}",
            )
            add(meeting.start_minute, f"start_{meeting.activity_id}")
            add(meeting.end_minute, f"end_{meeting.activity_id}")
        if self.spec.interaction is not None:
            add(self.spec.interaction.minute, "interaction")
        for index, batch in enumerate(self.spec.print_batches, start=1):
            add(batch.reveal_minute, f"print_request_{index:02d}")
            add(batch.submit_minute, f"print_submit_{index:02d}")
            add(batch.ready_by_minute, f"print_deadline_{index:02d}")
        add(self.spec.duration_minutes, "final_audit")
        return tuple(
            WorkflowCheckpoint(minute, tuple(items))
            for minute, items in sorted(labels.items())
        )

    def _checkpoint_label(self, checkpoint: WorkflowCheckpoint) -> str:
        return "_".join(checkpoint.labels)[:80]

    def _is_interaction_checkpoint(self, minute: int) -> bool:
        return (
            self.spec.interaction is not None and self.spec.interaction.minute == minute
        )

    def _service_counts_at(self, minute: int) -> dict[str, int]:
        """Reconstruct current occupancy plus rooms inside preparation windows."""

        events: list[tuple[int, int, str, int]] = []
        for phase in self.spec.phases:
            for room_id, count in phase.counts:
                events.append((phase.minute, 0, room_id, count))
        for meeting in self.spec.meetings:
            events.append((meeting.start_minute, 1, meeting.room_id, meeting.attendees))
            events.append((meeting.end_minute, 1, meeting.room_id, 0))
        counts: dict[str, int] = {}
        for event_minute, _, room_id, count in sorted(events):
            if event_minute > minute:
                break
            counts[room_id] = count
        for meeting in self.spec.meetings:
            if (
                meeting.start_minute - self.spec.preparation_lead_minutes
                <= minute
                < meeting.end_minute
            ):
                counts[meeting.room_id] = max(
                    counts.get(meeting.room_id, 0), meeting.attendees
                )
        # A hot/cold-weather room migration is actionable as soon as the
        # schedule-change event is observed. Starting the replacement room
        # immediately avoids carrying the obsolete room's thermal plan until
        # the generic preparation boundary.
        interaction = self.spec.interaction
        if (
            interaction is not None
            and interaction.minute <= minute
            and self.spec.hvac_mode in {"cooling", "heating"}
        ):
            changed_meeting = next(
                (
                    meeting
                    for meeting in self.spec.meetings
                    if meeting.activity_id == interaction.subject_id
                ),
                None,
            )
            if changed_meeting is not None and minute < changed_meeting.end_minute:
                counts[changed_meeting.room_id] = max(
                    counts.get(changed_meeting.room_id, 0),
                    changed_meeting.attendees,
                )
        # The opening-preparation checkpoint needs the upcoming design load,
        # even though the exogenous occupancy event has not occurred yet.
        for room_id in self.spec.rooms:
            if counts.get(room_id, 0) > 0:
                continue
            next_phase = next(
                (
                    phase
                    for phase in self.spec.phases
                    if minute < phase.minute <= minute + 30
                    and phase.occupancy.get(room_id, 0) > 0
                ),
                None,
            )
            if next_phase is not None:
                counts[room_id] = next_phase.occupancy[room_id]
        return {room_id: count for room_id, count in counts.items() if count > 0}

    def _room_policy(
        self,
        room_id: str,
        occupancy: int,
        minute: int,
        label: str,
        active_room_count: int,
    ) -> RoomControlPolicy:
        load_ratio = occupancy / ROOM_CAPACITIES[room_id]
        hvac_level = 1 if load_ratio < 0.35 else 2 if load_ratio < 0.75 else 3
        # Dedicated outdoor-air ventilation is deliberately staged below HVAC
        # fan speed. Low loads can rely on infiltration plus HVAC outdoor air;
        # forcing level 1 at all times caused severe humidity/heat-loss artifacts.
        ventilation_level = 0 if load_ratio < 0.35 else 1 if load_ratio < 0.75 else 2
        hvac_mode = self.spec.hvac_mode
        if "precondition" in label or "prepare_" in label:
            # Avoid treating every preconditioning window as maximum demand.
            # Cold starts need full heating authority; otherwise level 2 is
            # sufficient unless the upcoming design load already requires 3.
            minimum_preparation_level = 3 if hvac_mode == "heating" else 2
            hvac_level = max(hvac_level, minimum_preparation_level)
        # Under a binding whole-building power limit, preserve cooling/heating
        # semantics but cap lower-priority rooms. Switching them to plain fan
        # allowed the power audit to pass while room temperatures became unsafe.
        if self.spec.power_limit_w is not None and (
            (active_room_count >= 2 and room_id == "k1324")
            or (active_room_count >= 3 and room_id == "k1316")
        ):
            hvac_level = 1
            ventilation_level = 1
        if hvac_mode == "heating":
            ventilation_level = min(ventilation_level, 1)
        if self.spec.use_purifier:
            ventilation_level = 1
        scene = self._scene_for(room_id, label)
        return RoomControlPolicy(
            hvac_mode=hvac_mode,
            hvac_level=hvac_level,
            ventilation_level=ventilation_level,
            humidifier_level=(2 if self.spec.use_humidifier else 0),
            purifier_level=(2 if self.spec.use_purifier else 0),
            scene=scene,
        )

    def _scene_for(self, room_id: str, label: str) -> str:
        if not self.spec.use_presentation:
            return "off"
        if room_id == "k1316":
            return "seminar"
        if room_id != "k1315":
            return "off"
        for token in (
            "materials",
            "presentation",
            "questions",
            "closed_review",
            "summary",
        ):
            if token in label:
                return token
        return "conference"

    def _capture_room_control(
        self,
        previous,
        room_id,
        policy,
        step_prefix,
        hvac,
        air,
        ventilation,
        lighting,
        equipment,
    ):
        """Apply one changed room policy; unchanged policies emit no commands."""

        previous = (
            hvac.set_hvac(
                f"{room_id}_hvac_01",
                True,
                policy.hvac_mode,
                self.spec.target_temperature_c,
                policy.hvac_level,
            )
            .oracle()
            .with_id(f"{step_prefix}_prepare_{room_id}_hvac")
            .depends_on(previous, delay_seconds=1)
        )
        previous = (
            ventilation.set_ventilation(
                f"{room_id}_ventilation_01",
                policy.ventilation_level > 0,
                policy.ventilation_level,
            )
            .oracle()
            .with_id(f"{step_prefix}_prepare_{room_id}_ventilation")
            .depends_on(previous, delay_seconds=1)
        )
        if policy.humidifier_level and room_id in {"k1324", "k1315"}:
            previous = (
                air.set_humidifier(
                    f"{room_id}_humidifier_01", True, policy.humidifier_level
                )
                .oracle()
                .with_id(f"{step_prefix}_prepare_{room_id}_humidifier")
                .depends_on(previous, delay_seconds=1)
            )
        if policy.purifier_level and room_id in {"k1324", "k1315"}:
            previous = (
                air.set_air_purifier(
                    f"{room_id}_purifier_01", True, policy.purifier_level
                )
                .oracle()
                .with_id(f"{step_prefix}_prepare_{room_id}_purifier")
                .depends_on(previous, delay_seconds=1)
            )
        if policy.scene != "off":
            previous = self._capture_presentation_setup(
                previous, room_id, policy.scene, step_prefix, lighting, equipment
            )
        return previous

    def _capture_presentation_setup(
        self, previous, room_id, scene, step_prefix, lighting, equipment
    ):
        if room_id == "k1316":
            previous = (
                lighting.set_lighting("k1316_lighting_01", True, 65, 4000, scene)
                .oracle()
                .with_id(f"{step_prefix}_prepare_k1316_lighting")
                .depends_on(previous, delay_seconds=1)
            )
            previous = (
                equipment.set_projector("k1316_projector_01", True, "hdmi")
                .oracle()
                .with_id(f"{step_prefix}_prepare_k1316_projector")
                .depends_on(previous, delay_seconds=1)
            )
        elif room_id == "k1315":
            brightness_by_scene = {
                "materials": (70, 35),
                "presentation": (40, 65),
                "questions": (65, 75),
                "closed_review": (30, 60),
                "summary": (55, 70),
                "conference": (45, 70),
            }
            front, audience = brightness_by_scene.get(scene, (45, 70))
            for suffix, brightness in (("front", front), ("audience", audience)):
                previous = (
                    lighting.set_lighting(
                        f"k1315_lighting_{suffix}_01",
                        True,
                        brightness,
                        4000,
                        scene,
                    )
                    .oracle()
                    .with_id(f"{step_prefix}_prepare_k1315_{suffix}_lighting")
                    .depends_on(previous, delay_seconds=1)
                )
            previous = (
                equipment.set_projector("k1315_projector_01", True, "hdmi")
                .oracle()
                .with_id(f"{step_prefix}_prepare_k1315_projector")
                .depends_on(previous, delay_seconds=1)
            )
            previous = (
                equipment.set_audio_system("k1315_audio_01", True, 55, True)
                .oracle()
                .with_id(f"{step_prefix}_prepare_k1315_audio")
                .depends_on(previous, delay_seconds=1)
            )
        return previous

    def _capture_feedback_boost(
        self,
        previous,
        room_id,
        step_prefix,
        hvac,
        air,
        ventilation,
    ):
        """Represent the second feedback decision for a high-load room."""

        if self.spec.use_purifier and room_id in {"k1324", "k1315"}:
            previous = (
                air.set_air_purifier(f"{room_id}_purifier_01", True, 3)
                .oracle()
                .with_id(f"{step_prefix}_boost_{room_id}_purifier")
                .depends_on(previous, delay_seconds=1)
            )
        else:
            boost_level = 1 if self.spec.hvac_mode == "heating" else 2
            previous = (
                ventilation.set_ventilation(
                    f"{room_id}_ventilation_01", True, boost_level
                )
                .oracle()
                .with_id(f"{step_prefix}_boost_{room_id}_ventilation")
                .depends_on(previous, delay_seconds=1)
            )
        if self.spec.hvac_mode == "heating":
            previous = (
                hvac.set_hvac(
                    f"{room_id}_hvac_01",
                    True,
                    "heating",
                    self.spec.target_temperature_c,
                    3,
                )
                .oracle()
                .with_id(f"{step_prefix}_support_{room_id}_heating")
                .depends_on(previous, delay_seconds=1)
            )
        return previous

    def _capture_room_shutdown(
        self,
        previous,
        room_id,
        step_prefix,
        hvac,
        air,
        ventilation,
        lighting,
        equipment,
    ):
        """Close only devices that this scenario policy could have activated."""

        calls = [
            ("hvac", hvac.set_hvac(f"{room_id}_hvac_01", False, "off", 22.0, 0)),
            (
                "ventilation",
                ventilation.set_ventilation(f"{room_id}_ventilation_01", False, 0),
            ),
        ]
        if self.spec.use_humidifier and room_id in {"k1324", "k1315"}:
            calls.append(
                (
                    "humidifier",
                    air.set_humidifier(f"{room_id}_humidifier_01", False, 0),
                )
            )
        if self.spec.use_purifier and room_id in {"k1324", "k1315"}:
            calls.append(
                (
                    "purifier",
                    air.set_air_purifier(f"{room_id}_purifier_01", False, 0),
                )
            )
        if self.spec.use_presentation and room_id == "k1316":
            calls.extend(
                (
                    (
                        "lighting",
                        lighting.set_lighting(
                            "k1316_lighting_01", False, 0, 4000, "off"
                        ),
                    ),
                    (
                        "projector",
                        equipment.set_projector("k1316_projector_01", False, "off"),
                    ),
                )
            )
        if self.spec.use_presentation and room_id == "k1315":
            calls.extend(
                (
                    (
                        "front_lighting",
                        lighting.set_lighting(
                            "k1315_lighting_front_01", False, 0, 4000, "off"
                        ),
                    ),
                    (
                        "audience_lighting",
                        lighting.set_lighting(
                            "k1315_lighting_audience_01", False, 0, 4000, "off"
                        ),
                    ),
                    (
                        "projector",
                        equipment.set_projector("k1315_projector_01", False, "off"),
                    ),
                    (
                        "audio",
                        equipment.set_audio_system("k1315_audio_01", False, 0, False),
                    ),
                )
            )
        for suffix, call in calls:
            previous = (
                call.oracle()
                .with_id(f"{step_prefix}_shutdown_{room_id}_{suffix}")
                .depends_on(previous, delay_seconds=1)
            )
        return previous

    def validate(self, env=None) -> ScenarioValidationResult:
        world = self.get_typed_app(BuildingWorldApp)
        printing = self.get_typed_app(PrintingApp)
        meetings_complete = all(
            item.status == MeetingStatus.COMPLETED for item in world.schedule.values()
        )
        incomplete_meeting_ids = [
            item.meeting_id
            for item in world.schedule.values()
            if item.status != MeetingStatus.COMPLETED
        ]
        reservations_released = all(
            item.status == ReservationStatus.RELEASED
            for item in world.reservations.values()
        )
        active_reservation_ids = [
            item.reservation_id
            for item in world.reservations.values()
            if item.status != ReservationStatus.RELEASED
        ]
        all_rooms_empty = all(
            state.occupancy_count == 0 for state in world.room_states.values()
        )
        occupied_rooms = {
            room_id: state.occupancy_count
            for room_id, state in world.room_states.items()
            if state.occupancy_count != 0
        }
        all_devices_off = all(
            not state.power_on for state in world.device_states.values()
        )
        active_device_ids = [
            device_id
            for device_id, state in world.device_states.items()
            if state.power_on
        ]
        print_jobs_complete = all(
            item.get("status") == "completed" for item in printing.jobs.values()
        )
        incomplete_print_job_ids = [
            str(item.get("job_id"))
            for item in printing.jobs.values()
            if item.get("status") != "completed"
        ]
        event_types = {event.event_type for event in world.events}
        interaction_seen = self.spec.interaction is None or (
            self.spec.interaction.event_type in event_types
        )
        physics_steps = sum(
            item.get("kind") == "physics_step" for item in self.building_runtime.trace
        )
        power_samples = [
            (
                float(item.get("timestep_seconds", 0.0)),
                sum(
                    float(zone.get("electric_power_w", 0.0))
                    for zone in item.get("zones", [])
                ),
            )
            for item in self.building_runtime.trace
            if item.get("kind") == "physics_step"
        ]
        peak_power_w = max((power for _, power in power_samples), default=0.0)
        power_violation_seconds = sum(
            seconds
            for seconds, power in power_samples
            if self.spec.power_limit_w is not None and power > self.spec.power_limit_w
        )
        occupied_samples = [
            (
                float(item.get("timestep_seconds", 0.0)),
                str(item.get("at_time", "")),
                zone,
            )
            for item in self.building_runtime.trace
            if item.get("kind") == "physics_step"
            for zone in item.get("zones", [])
            if int(zone.get("occupancy_count", 0)) > 0
        ]
        occupied_seconds = sum(seconds for seconds, _, _ in occupied_samples)
        occupied_comfort_violation_seconds = sum(
            seconds
            for seconds, _, zone in occupied_samples
            if float(zone.get("comfort_score", 0.0)) < 0.7
        )
        comfort_violation_ratio = (
            occupied_comfort_violation_seconds / occupied_seconds
            if occupied_seconds > 0.0
            else 0.0
        )
        peak_co2_sample = max(
            occupied_samples,
            key=lambda sample: float(sample[2].get("co2_ppm", 0.0)),
            default=None,
        )
        peak_pm25_sample = max(
            occupied_samples,
            key=lambda sample: float(sample[2].get("pm25_ug_m3", 0.0)),
            default=None,
        )
        peak_occupied_co2_ppm = (
            float(peak_co2_sample[2].get("co2_ppm", 0.0))
            if peak_co2_sample is not None
            else 0.0
        )
        peak_occupied_pm25_ug_m3 = (
            float(peak_pm25_sample[2].get("pm25_ug_m3", 0.0))
            if peak_pm25_sample is not None
            else 0.0
        )
        peak_co2_location = (
            {
                "zone_id": peak_co2_sample[2].get("zone_id"),
                "at_time": peak_co2_sample[1],
            }
            if peak_co2_sample is not None
            else None
        )
        peak_pm25_location = (
            {
                "zone_id": peak_pm25_sample[2].get("zone_id"),
                "at_time": peak_pm25_sample[1],
            }
            if peak_pm25_sample is not None
            else None
        )
        unoccupied_energy_kwh = sum(
            float(zone.get("energy_kwh", 0.0))
            for item in self.building_runtime.trace
            if item.get("kind") == "physics_step"
            for zone in item.get("zones", [])
            if int(zone.get("occupancy_count", 0)) == 0
        )
        environment_within_limits = (
            peak_occupied_co2_ppm <= self.spec.evaluation.max_peak_co2_ppm
            and peak_occupied_pm25_ug_m3 <= self.spec.evaluation.max_peak_pm25_ug_m3
            and comfort_violation_ratio
            <= self.spec.evaluation.max_occupied_comfort_violation_ratio
        )
        unoccupied_energy_within_budget = (
            self.spec.evaluation.max_unoccupied_energy_kwh is None
            or unoccupied_energy_kwh <= self.spec.evaluation.max_unoccupied_energy_kwh
        )
        meeting_readiness = {
            meeting.activity_id: self._meeting_was_prepared(world, meeting)
            for meeting in self.spec.meetings
        }
        meetings_prepared = all(meeting_readiness.values())
        print_deadline_results = self._print_deadline_results(world, printing)
        print_deadlines_met = all(
            item["met"] for item in print_deadline_results
        )
        energy_by_zone = dict(
            self.building_runtime.physics.cumulative_energy_kwh_by_zone
        )
        next_scheduled_event_at = self.building_runtime.event_queue.next_time()
        minutes_to_next_event = (
            (next_scheduled_event_at - self.building_runtime.current_time).total_seconds()
            / 60.0
            if next_scheduled_event_at is not None
            else None
        )
        covered_rooms = {
            room_id
            for phase in self.spec.phases
            for room_id, count in phase.counts
            if count > 0
        }
        invariant_checks = {
            "meetings_complete": meetings_complete,
            "reservations_released": reservations_released,
            "all_rooms_empty": all_rooms_empty,
            "all_devices_off": all_devices_off,
            "print_jobs_complete": print_jobs_complete,
            "interaction_seen": interaction_seen,
            "physics_evolved": physics_steps > 0,
            "occupancy_covered": bool(covered_rooms),
            "power_within_limit": power_violation_seconds == 0.0,
            "environment_within_limits": environment_within_limits,
            "unoccupied_energy_within_budget": unoccupied_energy_within_budget,
            "meetings_prepared": meetings_prepared,
            "print_deadlines_met": print_deadlines_met,
        }
        failure_reasons: list[dict[str, object]] = []

        def fail(code: str, message: str, **details: object) -> None:
            if not invariant_checks[code]:
                failure_reasons.append(
                    {"code": code, "message": message, "details": details}
                )

        fail(
            "meetings_complete",
            "Some scheduled activities did not reach completed state.",
            meeting_ids=incomplete_meeting_ids,
        )
        fail(
            "reservations_released",
            "Some room reservations remain active or cancelled instead of released.",
            reservation_ids=active_reservation_ids,
        )
        fail(
            "all_rooms_empty",
            "End-of-day occupancy was not cleared.",
            occupied_rooms=occupied_rooms,
        )
        fail(
            "all_devices_off",
            "End-of-day device shutdown is incomplete.",
            device_ids=active_device_ids,
        )
        fail(
            "print_jobs_complete",
            "At least one submitted print job was not verified complete.",
            job_ids=incomplete_print_job_ids,
        )
        fail(
            "interaction_seen",
            "The run ended before the planned interaction event occurred.",
            expected_event=(
                self.spec.interaction.event_type.value
                if self.spec.interaction is not None
                else None
            ),
        )
        fail(
            "physics_evolved",
            "No physical simulation step was executed.",
            physics_steps=physics_steps,
        )
        fail(
            "occupancy_covered",
            "The scenario has no positively occupied room coverage.",
            rooms=sorted(covered_rooms),
        )
        fail(
            "power_within_limit",
            "The whole-building power limit was exceeded.",
            peak_power_w=peak_power_w,
            power_limit_w=self.spec.power_limit_w,
            violation_seconds=power_violation_seconds,
        )
        fail(
            "environment_within_limits",
            "Occupied-zone environmental limits were exceeded.",
            peak_co2_ppm=peak_occupied_co2_ppm,
            peak_co2_location=peak_co2_location,
            max_co2_ppm=self.spec.evaluation.max_peak_co2_ppm,
            peak_pm25_ug_m3=peak_occupied_pm25_ug_m3,
            peak_pm25_location=peak_pm25_location,
            max_pm25_ug_m3=self.spec.evaluation.max_peak_pm25_ug_m3,
            comfort_violation_ratio=comfort_violation_ratio,
            max_comfort_violation_ratio=(
                self.spec.evaluation.max_occupied_comfort_violation_ratio
            ),
        )
        fail(
            "unoccupied_energy_within_budget",
            "Unoccupied-room energy exceeded its budget.",
            energy_kwh=unoccupied_energy_kwh,
            budget_kwh=self.spec.evaluation.max_unoccupied_energy_kwh,
        )
        fail(
            "meetings_prepared",
            "Required room equipment was not powered before activity start.",
            unprepared_meeting_ids=[
                meeting_id
                for meeting_id, ready in meeting_readiness.items()
                if not ready
            ],
        )
        fail(
            "print_deadlines_met",
            "One or more public print requirements were missing, incorrect, or late.",
            results=print_deadline_results,
        )
        success = (
            meetings_complete
            and reservations_released
            and all_rooms_empty
            and all_devices_off
            and print_jobs_complete
            and interaction_seen
            and physics_steps > 0
            and bool(covered_rooms)
            and power_violation_seconds == 0.0
            and environment_within_limits
            and unoccupied_energy_within_budget
            and meetings_prepared
            and print_deadlines_met
        )
        return ScenarioValidationResult(
            success=success,
            rationale=(
                "full L3 lifecycle, physical evolution and final audit completed"
                if success
                else "failed invariants: "
                + ", ".join(item["code"] for item in failure_reasons)
            ),
            metadata={
                "scenario_number": self.spec.number,
                "family": self.spec.family,
                "outdoor_profile": self.spec.outdoor.profile_id,
                "episodes": list(dict.fromkeys(p.episode for p in self.spec.phases)),
                "rooms": list(self.spec.rooms),
                "meetings_complete": meetings_complete,
                "reservations_released": reservations_released,
                "all_rooms_empty": all_rooms_empty,
                "all_devices_off": all_devices_off,
                "print_jobs_complete": print_jobs_complete,
                "interaction_seen": interaction_seen,
                "physics_steps": physics_steps,
                "peak_power_w": peak_power_w,
                "power_violation_seconds": power_violation_seconds,
                "occupied_comfort_violation_ratio": comfort_violation_ratio,
                "peak_occupied_co2_ppm": peak_occupied_co2_ppm,
                "peak_occupied_co2_location": peak_co2_location,
                "peak_occupied_pm25_ug_m3": peak_occupied_pm25_ug_m3,
                "peak_occupied_pm25_location": peak_pm25_location,
                "environment_within_limits": environment_within_limits,
                "unoccupied_energy_kwh": unoccupied_energy_kwh,
                "unoccupied_energy_budget_kwh": (
                    self.spec.evaluation.max_unoccupied_energy_kwh
                ),
                "unoccupied_energy_within_budget": unoccupied_energy_within_budget,
                "meeting_readiness": meeting_readiness,
                "meetings_prepared": meetings_prepared,
                "print_deadlines_met": print_deadlines_met,
                "print_deadline_results": print_deadline_results,
                "failed_invariants": [
                    item["code"] for item in failure_reasons
                ],
                "failure_reasons": failure_reasons,
                "incomplete_meeting_ids": incomplete_meeting_ids,
                "active_reservation_ids": active_reservation_ids,
                "occupied_rooms": occupied_rooms,
                "active_device_ids": active_device_ids,
                "incomplete_print_job_ids": incomplete_print_job_ids,
                "energy_kwh_by_zone": energy_by_zone,
                "power_limit_w": self.spec.power_limit_w,
                "primary_metrics": list(self.spec.primary_metrics),
                "runtime_ended_at": self.building_runtime.current_time.isoformat(),
                "scenario_end_at": self.spec.end_at.isoformat(),
                "next_scheduled_event_at": (
                    next_scheduled_event_at.isoformat()
                    if next_scheduled_event_at is not None
                    else None
                ),
                "minutes_to_next_scheduled_event": minutes_to_next_event,
            },
        )

    def _meeting_was_prepared(self, world: BuildingWorldApp, meeting) -> bool:
        required_devices = {
            f"{meeting.room_id}_hvac_01",
            f"{meeting.room_id}_ventilation_01",
        }
        if "projector" in meeting.capabilities:
            required_devices.add(f"{meeting.room_id}_projector_01")
        if {"audio", "microphone"} & set(meeting.capabilities):
            required_devices.add(f"{meeting.room_id}_audio_01")
        start_at = self.spec.start_at + timedelta(minutes=meeting.start_minute)
        latest_power_state: dict[str, bool] = {}
        for event in world.events:
            if (
                event.event_type == BuildingEventType.DEVICE_STATE_CHANGED
                and event.occurred_at <= start_at
                and event.subject_id in required_devices
            ):
                latest_power_state[event.subject_id] = bool(
                    event.payload.get("power_on")
                )
        powered_at_start = {
            device_id
            for device_id, power_on in latest_power_state.items()
            if power_on
        }
        return required_devices <= powered_at_start

    def _print_deadline_results(
        self, world: BuildingWorldApp, printing: PrintingApp
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for batch_index, batch in enumerate(self.spec.print_batches, start=1):
            matching_jobs = [
                job
                for job in printing.jobs.values()
                if job.get("document_name") == batch.document_name
                and job.get("copies") == batch.copies
                and job.get("pages_per_copy") == batch.pages_per_copy
                and job.get("priority") == batch.priority
            ]
            job = matching_jobs[0] if len(matching_jobs) == 1 else None
            completion_events = [] if job is None else [
                event
                for event in world.events
                if event.event_type == BuildingEventType.PRINT_JOB_COMPLETED
                and event.subject_id == job["job_id"]
            ]
            deadline = self.spec.start_at + timedelta(minutes=batch.ready_by_minute)
            completed_at = (
                completion_events[-1].occurred_at if completion_events else None
            )
            exact_single_job = job is not None and len(matching_jobs) == 1
            completed = exact_single_job and job.get("status") == "completed"
            on_time = (
                completed_at is not None
                and completed_at <= deadline + timedelta(minutes=2)
            )
            request_id = self._print_request_id(batch_index)
            linked_request = printing.requests.get(request_id)
            met = bool(
                exact_single_job
                and completed
                and on_time
                and linked_request is not None
                and job.get("request_id") == request_id
            )
            reason = "met"
            if not exact_single_job:
                reason = "missing_or_non_unique_exact_job"
            elif not completed:
                reason = "job_not_completed"
            elif not on_time:
                reason = "completion_after_deadline_or_unverified"
            elif linked_request is None or job.get("request_id") != request_id:
                reason = "job_not_linked_to_public_request"
            results.append(
                {
                    "request_id": request_id,
                    "document_name": batch.document_name,
                    "expected": {
                        "copies": batch.copies,
                        "pages_per_copy": batch.pages_per_copy,
                        "priority": batch.priority,
                        "ready_by": deadline.isoformat(),
                    },
                    "matching_job_ids": [
                        str(item.get("job_id")) for item in matching_jobs
                    ],
                    "completed_at": (
                        completed_at.isoformat() if completed_at is not None else None
                    ),
                    "lateness_minutes": (
                        max(
                            0.0,
                            (completed_at - deadline).total_seconds() / 60.0,
                        )
                        if completed_at is not None
                        else None
                    ),
                    "met": met,
                    "reason": reason,
                }
            )
        return results

    def _build_briefing(self) -> str:
        public_meetings: list[str] = []
        for item in self.spec.meetings:
            payload = self._interaction_payload_for(item.activity_id)
            room_id = str(payload.get("from_room", item.room_id))
            attendees = int(payload.get("old_attendees", item.attendees))
            start_minute = int(payload.get("old_start_minute", item.start_minute))
            # Interaction scenario titles are deliberately neutral: names such
            # as "advanced" or "downsized" reveal a future change before its
            # domain event exists.
            title = (
                f"预约活动 {item.activity_id}"
                if payload
                else item.title
            )
            public_meetings.append(
                f"{title}({room_id}, {attendees}人, 第{start_minute}分钟开始)"
            )
        meetings = ", ".join(public_meetings) or "无预约会议，以办公室占用为主"
        interaction = (
            "运行中可能收到正常计划变化；只能依据届时已发生的事件局部重规划。"
            if self.spec.interaction is not None
            else "全天计划已确认，但仍需按阶段复核实际占用与环境。"
        )
        constraint = (
            f"同时遵守 {self.spec.power_limit_w:.0f} W 全楼功率约束。"
            if self.spec.power_limit_w is not None
            else "在满足业务与舒适目标的同时避免无效空房能耗。"
        )
        return (
            f"执行科创楼全天智慧建筑任务。当前已知活动：{meetings}。"
            f"{interaction}{constraint}"
            "如存在打印任务，必须从 PrintingApp.get_print_requests 读取公开的"
            "文件名、份数、页数、优先级和截止时间，不得猜测。"
            "使用 BuildingOperationsApp.get_operational_status 跟踪运行边界和未完成"
            "承诺；每次唤醒后使用 get_commitment_status 按截止时间先处理紧急承诺；"
            "该状态 can_finish=true 之前不得结束。会前必须使用 get_meeting_readiness"
            "完成全部设备清单；控制后使用 get_environment_control_status 连续复核，"
            "有人且未稳定时按建议间隔继续闭环。"
            "若存在全楼功率约束，每批设备变更后必须调用 get_power_budget_status，"
            "within_limit=false 时先降低或错峰低优先级负载，不得推进时间。"
            "请按 Observe→Reconcile→Prioritize→Act→Wait→Verify→Commit→Monitor "
            "协议运行，设备命令后必须推进物理时间并复查；日终清空房间、释放预约、"
            "关闭不再需要的设备并报告结果。"
        )


def _apply_schedule_interaction(
    scheduled: ScheduledBuildingEvent,
    world: BuildingWorldApp,
) -> None:
    """Apply a normal plan patch when (and only when) its event becomes due."""

    meeting = world.schedule.get(scheduled.subject_id)
    if meeting is None:
        return
    payload = scheduled.payload
    meeting.room_id = str(payload.get("room_id", meeting.room_id))
    meeting.expected_attendees = int(
        payload.get("attendees", meeting.expected_attendees)
    )
    if "start_at" in payload:
        meeting.start_at = datetime.fromisoformat(str(payload["start_at"]))
    if "end_at" in payload:
        meeting.end_at = datetime.fromisoformat(str(payload["end_at"]))
    if "required_capabilities" in payload:
        meeting.required_capabilities = tuple(payload["required_capabilities"])
    reservation = (
        world.reservations.get(meeting.reservation_id)
        if meeting.reservation_id is not None
        else None
    )
    if reservation is not None:
        reservation.resource_id = meeting.room_id
        reservation.start_at = meeting.start_at
        reservation.end_at = meeting.end_at


def _apply_print_request(
    scheduled: ScheduledBuildingEvent,
    printing: PrintingApp,
) -> None:
    """Expose a newly received material request at its actual event time."""

    payload = scheduled.payload
    printing.add_print_request(
        request_id=str(payload["request_id"]),
        document_name=str(payload["document_name"]),
        copies=int(payload["copies"]),
        pages_per_copy=int(payload["pages_per_copy"]),
        requested_by=str(payload["requested_by"]),
        submit_at=datetime.fromisoformat(str(payload["submit_at"])),
        ready_by=datetime.fromisoformat(str(payload["ready_by"])),
        priority=int(payload["priority"]),
    )


def _same_time_event_priority(event_type: BuildingEventType) -> int:
    """Keep plan changes ahead of phases derived from the changed plan."""

    if event_type in {
        BuildingEventType.SCHEDULE_CHANGED,
        BuildingEventType.MEETING_ENDED_EARLY,
    }:
        return 0
    if event_type == BuildingEventType.MEETING_PREPARATION_DUE:
        return 1
    return 2
