"""Runtime for manifest-driven, independently executable Building L1s."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Any

from fairy.apps.agent_user_interface import AgentUserInterface
from fairy.apps.building_world import (
    AirDeviceApp,
    BuildingOperationsApp,
    BuildingEventType,
    BuildingWorldApp,
    DeviceHealth,
    HvacApp,
    LightingApp,
    MeetingEquipmentApp,
    MeetingStatus,
    OccupancyApp,
    PrintingApp,
    ReservationStatus,
    ResourceAllocationApp,
    ResourceReservation,
    RoomApp,
    ScheduleApp,
    ScheduleEntry,
    VentilationApp,
)
from fairy.apps.system import SystemApp
from fairy.scenarios.building_kechuang.base import (
    KechuangBuildingScenario,
    collect_event_graph,
)
from fairy.scenarios.building_kechuang.l1.manifest import WRITE_TOOLS, load_contract
from fairy.scenarios.validation_result import ScenarioValidationResult
from fairy.types import EventRegisterer

APP_TYPES = {
    "AgentUserInterface": AgentUserInterface,
    "AirDeviceApp": AirDeviceApp,
    "BuildingOperationsApp": BuildingOperationsApp,
    "BuildingWorldApp": BuildingWorldApp,
    "HvacApp": HvacApp,
    "LightingApp": LightingApp,
    "MeetingEquipmentApp": MeetingEquipmentApp,
    "OccupancyApp": OccupancyApp,
    "PrintingApp": PrintingApp,
    "ResourceAllocationApp": ResourceAllocationApp,
    "RoomApp": RoomApp,
    "ScheduleApp": ScheduleApp,
    "SystemApp": SystemApp,
    "VentilationApp": VentilationApp,
}


def contract_timestamp(contract: dict[str, Any]) -> float:
    return datetime.fromisoformat(contract["start_at"]).timestamp()


class ManifestDrivenBuildingL1Scenario(KechuangBuildingScenario):
    """Execute one reviewed L1 contract without replaying its source L3."""

    contract_id = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.contract_id:
            raise ValueError("generated L1 class must declare contract_id")
        self.contract = load_contract(self.contract_id)

    def initiate_scenario(self) -> None:
        super().initiate_scenario()
        self._install_initial_state()
        self._initial_device_states = copy.deepcopy(
            self.get_typed_app(BuildingWorldApp).device_states
        )
        world = self.get_typed_app(BuildingWorldApp)
        self._initial_schedule = copy.deepcopy(world.schedule)
        self._initial_reservations = copy.deepcopy(world.reservations)
        operations = self.contract.get("initial_state", {}).get("operations")
        if operations is not None:
            world = self.get_typed_app(BuildingWorldApp)
            printing = self.get_typed_app(PrintingApp)
            app = BuildingOperationsApp(
                world=world,
                runtime=self.building_runtime,
                printing=printing,
                operational_end_at=datetime.fromtimestamp(
                    float(self.start_time or 0.0), tz=self.building_runtime.current_time.tzinfo
                )
                + timedelta(seconds=int(self.duration or 0)),
                target_temperature_c=float(operations.get("target_temperature_c", 24.0)),
                max_co2_ppm=float(operations.get("max_co2_ppm", 1000.0)),
                max_pm25_ug_m3=float(operations.get("max_pm25_ug_m3", 50.0)),
                power_limit_w=(
                    float(operations["power_limit_w"])
                    if operations.get("power_limit_w") is not None
                    else None
                ),
            )
            # L1 contracts use this App for power/status queries only.  The
            # long-horizon L3 completion guard would require undoing the L1's
            # target device state before allowing the atomic task to finish.
            app.completion_guard_enabled = False
            self.apps.append(app)

    def _install_initial_state(self) -> None:
        fixture = self.contract["initial_state"]
        world = self.get_typed_app(BuildingWorldApp)
        for item in fixture.get("devices", []):
            device_id = item["device_id"]
            if device_id not in world.device_states:
                raise ValueError(f"unknown fixture device {device_id!r}")
            state = world.device_states[device_id]
            for key in (
                "power_on",
                "mode",
                "level",
                "target_temperature_c",
                "water_level_pct",
                "filter_life_pct",
            ):
                if key in item:
                    setattr(state, key, item[key])
            if "settings" in item:
                state.settings = copy.deepcopy(item["settings"])
            if "health" in item:
                state.health = DeviceHealth(item["health"])
        for item in fixture.get("rooms", []):
            room_id = item["room_id"]
            if room_id not in world.room_states:
                raise ValueError(f"unknown fixture room {room_id!r}")
            world.room_states[room_id].occupancy_count = int(
                item.get("occupancy_count", 0)
            )
        world.schedule = {
            item["meeting_id"]: ScheduleEntry(
                meeting_id=item["meeting_id"],
                room_id=item["room_id"],
                organizer_id=item["organizer_id"],
                participant_ids=tuple(item.get("participant_ids", [])),
                start_at=datetime.fromisoformat(item["start_at"]),
                end_at=datetime.fromisoformat(item["end_at"]),
                expected_attendees=int(item["expected_attendees"]),
                status=MeetingStatus(item.get("status", MeetingStatus.CONFIRMED.value)),
                title=item.get("title", ""),
                required_capabilities=tuple(item.get("required_capabilities", [])),
                reservation_id=item.get("reservation_id"),
            )
            for item in fixture.get("schedule", [])
        }
        world.reservations = {
            item["reservation_id"]: ResourceReservation(
                reservation_id=item["reservation_id"],
                resource_id=item["resource_id"],
                owner_id=item["owner_id"],
                start_at=datetime.fromisoformat(item["start_at"]),
                end_at=datetime.fromisoformat(item["end_at"]),
                status=ReservationStatus(
                    item.get("status", ReservationStatus.ACTIVE.value)
                ),
            )
            for item in fixture.get("reservations", [])
        }
        printing = self.get_typed_app(PrintingApp)
        printing.requests = {
            request["request_id"]: {
                **copy.deepcopy(request),
                "status": request.get("status", "pending"),
                "job_id": request.get("job_id"),
            }
            for request in fixture.get("print_requests", [])
        }
        printing.jobs = {
            job["job_id"]: copy.deepcopy(job)
            for job in fixture.get("print_jobs", [])
        }
        if "id_counters" in fixture:
            world._id_counters = {
                str(key): int(value)
                for key, value in fixture["id_counters"].items()
            }
        for job in printing.jobs.values():
            if job.get("status") == "completed":
                continue
            ready_at = datetime.fromtimestamp(
                float(job["ready_at_timestamp"]),
                tz=self.building_runtime.current_time.tzinfo,
            )
            if ready_at < self.building_runtime.current_time:
                continue
            self.building_runtime.schedule_event(
                scheduled_id=f"complete-{job['job_id']}",
                execute_at=ready_at,
                event_type=BuildingEventType.PRINT_JOB_COMPLETED,
                source=printing.name,
                subject_id=job["job_id"],
                payload={"printer_id": job["printer_id"]},
            )

    def build_events_flow(self) -> None:
        aui = self.get_typed_app(AgentUserInterface)
        with EventRegisterer.capture_mode():
            briefing = (
                aui.send_message_to_agent(content=self.scenario_input)
                .with_id("briefing")
                .depends_on(None, delay_seconds=5)
            )
            previous = briefing
            for step in self.contract["oracle_steps"]:
                app_name, method_name = step["tool"].split("__", 1)
                app_type = APP_TYPES[app_name]
                app = self.get_typed_app(app_type)
                event = getattr(app, method_name)(**step.get("args", {}))
                event = event.oracle().with_id(step["id"]).depends_on(
                    previous, delay_seconds=int(step.get("delay_seconds", 1))
                )
                previous = event
            (
                aui.send_message_to_user(
                    content=self.contract["task"]["completion_report"]
                )
                .oracle()
                .with_id("report_result")
                .depends_on(previous, delay_seconds=1)
            )
            self.events = collect_event_graph(briefing)

    def validate(self, env) -> ScenarioValidationResult:
        validation = self.contract["validation"]
        if validation["kind"] == "printing":
            business = self._validate_printing(validation)
        elif validation["kind"] == "meeting_booking":
            business = self._validate_meeting_booking(validation)
        elif validation["kind"] == "meeting_change":
            business = self._validate_meeting_change(validation)
        else:
            business = self._validate_device_state(validation)
        evidence = self._validate_completion_evidence(env.workflow, validation)
        conditions = {**business, **evidence}
        success = all(bool(value) for value in conditions.values())
        return ScenarioValidationResult(
            success=success,
            rationale=(
                "reviewed L1 business goal and completion evidence satisfied"
                if success
                else "failed L1 conditions: "
                + ", ".join(key for key, value in conditions.items() if not value)
            ),
            metadata={
                "contract_id": self.contract_id,
                "behavior_family": self.contract["behavior_family"],
                "source_l3_id": self.contract["source"].get("l3_scenario_id"),
                "source_candidate_id": self.contract["source"].get("candidate_id"),
                "conditions": conditions,
            },
        )

    def _validate_device_state(self, validation: dict[str, Any]) -> dict[str, bool]:
        world = self.get_typed_app(BuildingWorldApp)
        expected_actions: dict[str, dict[str, Any]] = {}
        for step in self.contract["oracle_steps"]:
            if step["tool"] not in WRITE_TOOLS:
                continue
            device_id = step.get("args", {}).get("device_id")
            if device_id is not None:
                expected_actions[device_id] = step
        target_configuration = all(
            _device_matches(world.device_states[device_id], step)
            for device_id, step in expected_actions.items()
        )
        target_ids = set(expected_actions)
        untouched = all(
            state == self._initial_device_states[device_id]
            for device_id, state in world.device_states.items()
            if device_id not in target_ids
        )
        checks = {
            "target_configuration": target_configuration,
            "untargeted_devices_preserved": untouched,
            "schedule_preserved": world.schedule == self._initial_schedule,
            "reservations_preserved": (
                world.reservations == self._initial_reservations
            ),
        }
        if validation.get("power_limit_w") is not None:
            operations = self.get_typed_app(BuildingOperationsApp)
            budget = operations.get_power_budget_status()
            checks["within_power_limit"] = bool(budget["within_limit"])
        return checks

    def _validate_printing(self, validation: dict[str, Any]) -> dict[str, bool]:
        printing = self.get_typed_app(PrintingApp)
        world = self.get_typed_app(BuildingWorldApp)
        request_ids = list(validation["request_ids"])
        exact = True
        completed = True
        on_time = True
        linked_job_ids: list[str] = []
        completion_subjects = {
            event.subject_id
            for event in world.events
            if event.event_type.value == "print_job_completed"
        }
        completion_events = True
        target_device_ids = {
            step.get("args", {}).get("device_id")
            for step in self.contract["oracle_steps"]
            if step["tool"] == "PrintingApp__set_printer_power"
        } - {None}
        for request_id in request_ids:
            request = printing.requests.get(request_id)
            if request is None:
                exact = completed = on_time = completion_events = False
                continue
            jobs = [
                job
                for job in printing.jobs.values()
                if job.get("request_id") == request_id
            ]
            if len(jobs) != 1:
                exact = completed = on_time = completion_events = False
                continue
            job = jobs[0]
            linked_job_ids.append(str(job["job_id"]))
            exact = exact and all(
                job.get(key) == request.get(key)
                for key in (
                    "document_name",
                    "copies",
                    "pages_per_copy",
                    "requested_by",
                    "priority",
                )
            )
            completed = completed and job.get("status") == "completed"
            on_time = on_time and float(job["ready_at_timestamp"]) <= datetime.fromisoformat(
                request["ready_by"]
            ).timestamp()
            completion_events = completion_events and job["job_id"] in completion_subjects
        return {
            "all_requests_have_one_exact_linked_job": exact,
            "all_jobs_completed": completed,
            "all_jobs_on_time": on_time,
            "no_duplicate_linked_jobs": len(linked_job_ids) == len(set(linked_job_ids)),
            "completion_events_published": completion_events,
            "untargeted_devices_preserved": all(
                state == self._initial_device_states[device_id]
                for device_id, state in world.device_states.items()
                if device_id not in target_device_ids
            ),
            "schedule_preserved": world.schedule == self._initial_schedule,
            "reservations_preserved": (
                world.reservations == self._initial_reservations
            ),
        }

    def _validate_meeting_booking(self, validation: dict[str, Any]) -> dict[str, bool]:
        world = self.get_typed_app(BuildingWorldApp)
        expected = validation["expected_meeting"]
        initial_ids = set(self._initial_schedule)
        created = [
            meeting
            for meeting_id, meeting in world.schedule.items()
            if meeting_id not in initial_ids
            and meeting.status == MeetingStatus.CONFIRMED
        ]
        matching = [meeting for meeting in created if _meeting_matches(meeting, expected)]
        reservation_ok = False
        if len(matching) == 1 and matching[0].reservation_id is not None:
            reservation = world.reservations.get(matching[0].reservation_id)
            reservation_ok = bool(
                reservation is not None
                and reservation.status == ReservationStatus.ACTIVE
                and reservation.resource_id == matching[0].room_id
            )
        return {
            "one_matching_meeting_created": len(created) == len(matching) == 1,
            "active_reservation_created": reservation_ok,
            "existing_meetings_preserved": all(
                world.schedule.get(meeting_id) == meeting
                for meeting_id, meeting in self._initial_schedule.items()
            ),
            "existing_reservations_preserved": all(
                world.reservations.get(reservation_id) == reservation
                for reservation_id, reservation in self._initial_reservations.items()
            ),
            "devices_preserved": (
                world.device_states == self._initial_device_states
            ),
        }

    def _validate_meeting_change(self, validation: dict[str, Any]) -> dict[str, bool]:
        world = self.get_typed_app(BuildingWorldApp)
        old_meeting_id = validation["old_meeting_id"]
        old_reservation_id = validation["old_reservation_id"]
        old_meeting = world.schedule.get(old_meeting_id)
        old_reservation = world.reservations.get(old_reservation_id)
        created = [
            meeting
            for meeting_id, meeting in world.schedule.items()
            if meeting_id not in self._initial_schedule
            and meeting.status == MeetingStatus.CONFIRMED
        ]
        matching = [
            meeting
            for meeting in created
            if _meeting_matches(meeting, validation["expected_meeting"])
        ]
        replacement_reservation_ok = False
        if len(matching) == 1 and matching[0].reservation_id is not None:
            reservation = world.reservations.get(matching[0].reservation_id)
            replacement_reservation_ok = bool(
                reservation is not None
                and reservation.status == ReservationStatus.ACTIVE
                and reservation.resource_id == matching[0].room_id
            )
        excluded_meetings = {old_meeting_id}
        excluded_reservations = {old_reservation_id}
        return {
            "old_meeting_cancelled": bool(
                old_meeting is not None
                and old_meeting.status == MeetingStatus.CANCELLED
            ),
            "old_reservation_released": bool(
                old_reservation is not None
                and old_reservation.status == ReservationStatus.CANCELLED
            ),
            "one_matching_replacement_created": len(created) == len(matching) == 1,
            "replacement_reservation_active": replacement_reservation_ok,
            "unaffected_meetings_preserved": all(
                world.schedule.get(meeting_id) == meeting
                for meeting_id, meeting in self._initial_schedule.items()
                if meeting_id not in excluded_meetings
            ),
            "unaffected_reservations_preserved": all(
                world.reservations.get(reservation_id) == reservation
                for reservation_id, reservation in self._initial_reservations.items()
                if reservation_id not in excluded_reservations
            ),
            "devices_preserved": (
                world.device_states == self._initial_device_states
            ),
        }

    def _validate_completion_evidence(
        self, workflow, validation: dict[str, Any]
    ) -> dict[str, bool]:
        steps = list(workflow.dag.values())
        action_tools = {
            step["tool"]
            for step in self.contract["oracle_steps"]
            if step["tool"] in WRITE_TOOLS
        }
        action_positions = [
            index
            for index, step in enumerate(steps)
            if step.tool_name in action_tools
        ]
        last_action = max(action_positions, default=-1)
        direct_confirmations = {
            step.tool_name
            for index, step in enumerate(steps)
            if index > last_action and step.tool_name is not None
        }
        target_device_ids = {
            step.get("args", {}).get("device_id")
            for step in self.contract["oracle_steps"]
            if step["tool"] in WRITE_TOOLS
            and step.get("args", {}).get("device_id") is not None
        }
        observed_device_ids = {
            (step.tool_args or {}).get("device_id")
            for index, step in enumerate(steps)
            if index > last_action
            and step.tool_name == "DeviceRegistryApp__get_device_state"
        } - {None}
        confirmation_ok = all(
            tool in direct_confirmations
            or (
                validation["kind"] == "device_state"
                and tool == "BuildingWorldApp__get_building_overview"
                and bool(target_device_ids)
                and target_device_ids <= observed_device_ids
            )
            for tool in validation["confirmation_tools"]
        )
        report_ok = any(
            index > last_action
            and (
                step.tool_name == "AgentUserInterface__send_message_to_user"
                or (
                    step.op_type == "ASSISTANT"
                    and bool(str(step.content or "").strip())
                )
            )
            for index, step in enumerate(steps)
        )
        return {
            "completion_observed_after_actions": confirmation_ok,
            "result_reported_after_actions": report_ok,
        }


def _device_matches(state, step: dict[str, Any]) -> bool:
    args = step["args"]
    tool = step["tool"]
    if state.power_on != args["power_on"]:
        return False
    if not args["power_on"]:
        return state.mode == "off" and state.level == 0
    if tool == "HvacApp__set_hvac":
        return (
            state.mode == args["mode"]
            and state.target_temperature_c == args["target_temperature_c"]
            and state.level == args["fan_level"]
        )
    if tool == "VentilationApp__set_ventilation":
        return state.mode == "outdoor_air" and state.level == args["level"]
    if tool == "AirDeviceApp__set_air_purifier":
        return state.mode == "on" and state.level == args["level"]
    if tool == "AirDeviceApp__set_humidifier":
        return state.mode == "on" and state.level == args["level"]
    if tool == "PrintingApp__set_printer_power":
        return state.mode == "ready" and state.level == 1
    if tool == "LightingApp__set_lighting":
        return state.settings == {
            "brightness_pct": args["brightness_pct"],
            "color_temperature_k": args["color_temperature_k"],
            "scene": args["scene"],
        }
    if tool == "MeetingEquipmentApp__set_projector":
        return state.settings.get("input_source") == args["input_source"]
    if tool == "MeetingEquipmentApp__set_audio_system":
        return state.settings == {
            "volume_pct": args["volume_pct"],
            "microphone_enabled": args["microphone_enabled"],
        }
    return False


def _meeting_matches(meeting: ScheduleEntry, expected: dict[str, Any]) -> bool:
    return (
        meeting.room_id == expected["room_id"]
        and meeting.organizer_id == expected["organizer_id"]
        and set(meeting.participant_ids) == set(expected.get("participant_ids", []))
        and meeting.start_at.isoformat() == expected["start_at"]
        and meeting.end_at.isoformat() == expected["end_at"]
        and meeting.expected_attendees == int(expected["expected_attendees"])
        and set(meeting.required_capabilities)
        == set(expected.get("required_capabilities", []))
        and meeting.status == MeetingStatus.CONFIRMED
    )
