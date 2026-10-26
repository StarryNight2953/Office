"""Public operating contract and completion guard for long Building runs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from threading import RLock
from typing import Any

from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.device_plans import (
    COMMANDS,
    DeviceExpectation,
    RoomTask,
    canonical_goals,
    coalesce_room_tasks,
    execute_device_plan,
)
from fairy.apps.building_world.device_plans import (
    mismatches as device_goal_mismatches,
)
from fairy.apps.building_world.printing_app import PrintingApp
from fairy.apps.building_world.runtime import BuildingWorldRuntime
from fairy.apps.building_world.types import (
    BuildingEventType,
    DeviceHealth,
    DeviceSpec,
    DeviceState,
    DeviceType,
    MeetingStatus,
    ReservationStatus,
)
from fairy.apps.system import SystemApp
from fairy.physics.building.orchestrator import (
    CO2_WARNING_THRESHOLD_PPM,
    PM25_WARNING_THRESHOLD_UG_M3,
)
from fairy.physics.building.sensor_api import SensorQuantity
from fairy.tool_utils import OperationType, app_tool, data_tool
from fairy.types import event_registered
from fairy.utils.type_utils import type_check


class BuildingOperationsApp(App):
    """Expose what must remain true across a complete operational horizon.

    The App publishes policy and current observations, never simulator truth or
    future event payloads.  Its internal completion result is also consumed by
    the controller so a model cannot end while known commitments remain open.
    """

    completion_guard_enabled = True

    def __init__(
        self,
        *,
        world: BuildingWorldApp,
        runtime: BuildingWorldRuntime,
        printing: PrintingApp,
        system: SystemApp | None = None,
        operational_end_at: datetime,
        target_temperature_c: float,
        max_co2_ppm: float,
        max_pm25_ug_m3: float,
        power_limit_w: float | None = None,
        max_unoccupied_energy_kwh: float | None = None,
        preparation_lead_minutes: int = 60,
    ) -> None:
        super().__init__(name="BuildingOperationsApp")
        if operational_end_at.tzinfo is None:
            raise ValueError("operational_end_at must be timezone-aware")
        self.world = world
        self.runtime = runtime
        self.printing = printing
        self.system = system
        self.operational_end_at = operational_end_at
        self.target_temperature_c = float(target_temperature_c)
        self.max_co2_ppm = float(max_co2_ppm)
        self.max_pm25_ug_m3 = float(max_pm25_ug_m3)
        self.power_limit_w = (
            float(power_limit_w) if power_limit_w is not None else None
        )
        self.max_unoccupied_energy_kwh = max_unoccupied_energy_kwh
        self.preparation_lead_minutes = int(preparation_lead_minutes)
        self._last_environment_check_at: datetime | None = None
        self._last_environment_device_event_count = -1
        self._last_environment_status: dict[str, Any] | None = None
        self.fair_wait_enabled = False
        self._stagnant_wait_feedback: tuple[Any, ...] | None = None
        self.device_execution_lock = RLock()
        self.device_command_tools: dict[str, Any] = {}
        self.device_plan_actions: list[dict[str, Any]] = []
        self.reconciled_wait_event_count = 0
        self.routine_event_acknowledger = None

    def bind_device_tools(self, apps) -> None:
        """Bind existing command tools, never replace gateways or their guards."""
        self.device_command_tools = {
            tool.to_open_ai()["function"]["name"]: tool
            for app in apps if app is not self for tool in app.get_tools()
            if tool.to_open_ai()["function"]["name"] in COMMANDS.values()
        }

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def apply_device_plan(self, room_id: str, expected_devices: list[DeviceExpectation]) -> dict[str, Any]:
        """Execute explicit room device-state goals without model-per-device turns.

        Available equally in fair single/multi runs. Supply canonical modes:
        HVAC cooling/heating/fan, ventilation outdoor_air, air devices on,
        projector/audio/printer ready; off devices have mode off and level 0.
        Unknown goals are rejected before any command. Every real command uses
        the existing device tool and live safety preflight. This is NOT physical
        comfort verification. Inspect actions for partial execution on failure.
        """
        if not self.fair_wait_enabled:
            return {"status": "rejected", "reason": "fair_device_plan_disabled"}
        try:
            room = self.world.resolve_room_id(room_id) if isinstance(room_id, str) else None
            if room is None:
                raise ValueError("unknown room")
            goals = canonical_goals(self.world, room, expected_devices)
            if not goals:
                raise ValueError("explicit device goals are required")
        except ValueError as exc:
            return {"status": "rejected", "reason": "invalid_device_expectations", "details": str(exc)}
        with self.device_execution_lock:
            return execute_device_plan(self, room, goals, lambda name, args: self.device_command_tools[name](**args))

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def apply_room_plans(self, room_tasks: list[RoomTask], wait_minutes: int = 0) -> dict[str, Any]:
        """Apply complete explicit room plans and optionally perform ONE approved wait.

        task is an audit label; only expected_devices are executed. Compatible
        same-room goals are merged, conflicting goals rejected before mutation.
        List shared-load reductions before increases across different rooms.
        Wait 0..60 minutes only after ALL command goals verify, using the ordinary
        event/comfort/commitment/horizon guards. Meaningful events return control
        immediately. This does not approve any later plan or suppress failures.
        """
        if type(wait_minutes) is not int or not 0 <= wait_minutes <= 60:
            return {"status": "rejected", "reason": "plan_wait_bounds_invalid"}
        try:
            if not isinstance(room_tasks, list) or not 1 <= len(room_tasks) <= 64:
                raise ValueError("room_task_count_invalid")
            tasks = []
            for item in room_tasks:
                if not isinstance(item, dict) or set(item) - {"room_id", "task", "expected_devices"}:
                    raise ValueError("invalid_room_task")
                room = self.world.resolve_room_id(item.get("room_id", ""))
                if room is None or not isinstance(item.get("task"), str) or not item["task"].strip():
                    raise ValueError("invalid_room_task")
                goals = canonical_goals(self.world, room, item.get("expected_devices"))
                if not goals:
                    raise ValueError("explicit_device_goals_required")
                tasks.append({"room_id": room, "task": item["task"], "expected_devices": goals})
            tasks = coalesce_room_tasks(tasks)
            # Revalidate merged fields so partial goals cannot jointly create
            # an impossible powered-off/mode/level combination.
            for item in tasks:
                canonical_goals(self.world, item["room_id"], item["expected_devices"])
        except (ValueError, TypeError) as exc:
            return {"status": "rejected", "reason": "invalid_room_plan", "details": str(exc)}
        results = []
        for item in tasks:
            result = self.apply_device_plan(item["room_id"], item["expected_devices"])
            results.append({"room_id": item["room_id"], **result})
            if result["status"] != "ok":
                return {"status": "blocked", "reason": "room_plan_incomplete", "results": results, "wait": None}
        wait = self.guarded_wait(wait_minutes * 60, approved_room_goals={item["room_id"]: item["expected_devices"] for item in tasks}) if wait_minutes else None
        return {"status": "ok" if wait is None or wait["reason"] == "time_budget_reached" else "attention_required",
                "reason": "room_plans_verified", "results": results, "wait": wait,
                "coalesced_task_count": len(room_tasks) - len(tasks)}

    def enable_fair_wait_policy(self) -> None:
        """Apply the same bounded wait/horizon contract to both live agent modes."""

        if self.system is None:
            raise ValueError("fair wait policy requires SystemApp")
        self.fair_wait_enabled = True
        self.system.require_guarded_time_advance = True
        self.system.guarded_wait_handler = self.guarded_wait

    def closeout_progress_state(
        self, completion_status: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Material public blockers, deliberately excluding clocks and query counters."""

        if self.runtime.current_time < self.operational_end_at:
            return None
        status = (
            completion_status
            if completion_status is not None
            else self.completion_status()
        )
        if status["can_finish"]:
            return None
        return {"blockers": status["blockers"]}

    def guarded_wait(self, requested_seconds: int, *, approved_room_goals=None) -> dict[str, Any]:
        """Deterministic physical rechecks replace repeated model/read/wait turns.

        Never actuates devices; urgent work and all ordinary advance guards still
        stop the wait. Each request is bounded to one hour and the public horizon.
        """

        started_at = self.runtime.current_time
        result = self._monitor_environment(
            max_seconds=min(requested_seconds, 3600),
            interval_seconds=300,
            stop_when_stable=False,
            approved_room_goals=approved_room_goals,
        )
        advanced = (self.runtime.current_time - started_at).total_seconds()
        clock_result = result.get("time_advance") or {}
        return {
            "status": "ok" if advanced > 0 else "rejected",
            "reason": result["reason"],
            "requested_seconds": requested_seconds,
            "advanced_seconds": advanced,
            "current_timestamp": self.runtime.current_time.timestamp(),
            "current_datetime": self.runtime.current_time.isoformat(),
            "interrupted": (
                advanced < requested_seconds
                or bool(clock_result.get("interrupted"))
                or result["reason"] != "time_budget_reached"
            ),
            "interruptions": clock_result.get("interruptions", []),
            "bounded_wait": result,
            "required_action": (
                "Inspect the returned environment/guard details and resolve blockers. "
                "At the horizon, finish closeout instead of advancing again."
            ),
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_operational_status(self) -> dict[str, Any]:
        """Return horizon progress and blockers that must clear before finishing.

        The next scheduled timestamp is public, but its hidden event type and
        payload are not disclosed before occurrence.
        """

        return self.completion_status()

    def completion_status(self) -> dict[str, Any]:
        """Return the controller-facing completion decision without side effects."""

        now = self.runtime.current_time
        next_event_at = self.runtime.event_queue.next_time()
        occupied_rooms = {
            room_id: state.occupancy_count
            for room_id, state in self.world.room_states.items()
            if state.occupancy_count > 0
        }
        open_meetings = [
            meeting.meeting_id
            for meeting in self.world.schedule.values()
            if meeting.status == MeetingStatus.CONFIRMED
        ]
        active_reservations = [
            reservation.reservation_id
            for reservation in self.world.reservations.values()
            if reservation.status == ReservationStatus.ACTIVE
        ]
        unfinished_print_requests = [
            request_id
            for request_id, request in self.printing.requests.items()
            if request["status"] != "completed"
        ]
        active_devices = [
            device_id
            for device_id, state in self.world.device_states.items()
            if state.power_on
        ]
        blockers: list[dict[str, Any]] = []
        if now < self.operational_end_at:
            blockers.append(
                {
                    "code": "operational_horizon_not_reached",
                    "minutes_remaining": (
                        self.operational_end_at - now
                    ).total_seconds()
                    / 60.0,
                }
            )
        if next_event_at is not None and next_event_at <= self.operational_end_at:
            blockers.append(
                {
                    "code": "scheduled_event_pending",
                    "next_event_at": next_event_at.isoformat(),
                    "minutes_until_event": (next_event_at - now).total_seconds()
                    / 60.0,
                }
            )
        if occupied_rooms:
            blockers.append({"code": "rooms_occupied", "rooms": occupied_rooms})
        if open_meetings:
            blockers.append({"code": "meetings_open", "meeting_ids": open_meetings})
        if active_reservations:
            blockers.append(
                {
                    "code": "reservations_active",
                    "reservation_ids": active_reservations,
                }
            )
        if unfinished_print_requests:
            blockers.append(
                {
                    "code": "print_requests_unfinished",
                    "request_ids": unfinished_print_requests,
                }
            )
        if active_devices:
            blockers.append(
                {"code": "devices_still_on", "device_ids": active_devices}
            )
        # Operational completion and scenario validation are different contracts.
        # Report irreversible quality failures without making them blockers;
        # otherwise the agent could retry forever with no repair available.
        quality_alerts: list[dict[str, Any]] = []
        for meeting in self.world.schedule.values():
            if meeting.status == MeetingStatus.CANCELLED:
                quality_alerts.append(
                    {"code": "meeting_cancelled", "meeting_id": meeting.meeting_id}
                )
        for reservation in self.world.reservations.values():
            if reservation.status == ReservationStatus.CANCELLED:
                quality_alerts.append(
                    {
                        "code": "reservation_cancelled",
                        "reservation_id": reservation.reservation_id,
                    }
                )
        completed_print_at = {
            event.subject_id: event.occurred_at
            for event in self.world.events
            if event.event_type == BuildingEventType.PRINT_JOB_COMPLETED
        }
        for request_id, request in self.printing.requests.items():
            deadline = datetime.fromisoformat(str(request["ready_by"]))
            completed_at = completed_print_at.get(request.get("job_id"))
            if (
                completed_at is not None
                and completed_at > deadline + timedelta(minutes=2)
            ) or (
                completed_at is None
                and now > deadline + timedelta(minutes=2)
            ):
                quality_alerts.append(
                    {"code": "print_deadline_missed", "request_id": request_id}
                )
        return {
            "status": "ok",
            "can_finish": not blockers,
            "current_time": now.isoformat(),
            "operational_end_at": self.operational_end_at.isoformat(),
            "next_scheduled_event_at": (
                next_event_at.isoformat() if next_event_at is not None else None
            ),
            "blockers": blockers,
            "quality_alerts": quality_alerts,
            "completion_scope": "operational_only; scenario validation is separate",
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_meeting_readiness(self, meeting_id: str) -> dict[str, Any]:
        """Return the explicit device checklist for one currently known meeting."""

        meeting = self.world.schedule.get(meeting_id)
        if meeting is None or meeting.status != MeetingStatus.CONFIRMED:
            return {"error": f"unknown confirmed meeting_id {meeting_id!r}"}
        required_devices = {
            f"{meeting.room_id}_hvac_01": "thermal_control",
            f"{meeting.room_id}_ventilation_01": "outdoor_air_control",
        }
        if "projector" in meeting.required_capabilities:
            required_devices[f"{meeting.room_id}_projector_01"] = "projector"
        if {"audio", "microphone"} & set(meeting.required_capabilities):
            required_devices[f"{meeting.room_id}_audio_01"] = "audio_microphone"
        checklist = []
        for device_id, purpose in required_devices.items():
            state = self.world.device_states.get(device_id)
            checklist.append(
                {
                    "device_id": device_id,
                    "purpose": purpose,
                    "exists": state is not None,
                    "power_on": bool(state.power_on) if state is not None else False,
                    "ready": bool(state is not None and state.power_on),
                }
            )
        now = self.runtime.current_time
        return {
            "meeting_id": meeting_id,
            "room_id": meeting.room_id,
            "start_at": meeting.start_at.isoformat(),
            "end_at": meeting.end_at.isoformat(),
            "phase": ("upcoming" if now < meeting.start_at else "active" if now < meeting.end_at else "end_event_pending"),
            "minutes_until_end": (meeting.end_at - now).total_seconds() / 60.0,
            "minutes_until_start": (meeting.start_at - now).total_seconds() / 60.0,
            "required_capabilities": list(meeting.required_capabilities),
            "ready": all(item["ready"] for item in checklist),
            "checklist": checklist,
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_commitment_status(self) -> dict[str, Any]:
        """Return public commitments ordered by their next actionable deadline.

        This combines the public schedule and print queue so the controller can
        prioritize a near deadline before spending simulated time on lower-risk
        comfort tuning. It does not expose scheduled interaction events that
        have not happened yet.
        """

        now = self.runtime.current_time
        meetings = []
        for meeting in self.world.schedule.values():
            if meeting.status != MeetingStatus.CONFIRMED:
                continue
            readiness = self.get_meeting_readiness(meeting.meeting_id)
            minutes_until_start = (meeting.start_at - now).total_seconds() / 60.0
            meetings.append(
                {
                    "commitment_type": "meeting",
                    "meeting_id": meeting.meeting_id,
                    "room_id": meeting.room_id,
                    "start_at": meeting.start_at.isoformat(),
                    "end_at": meeting.end_at.isoformat(),
                    "phase": readiness["phase"],
                    "minutes_until_end": readiness["minutes_until_end"],
                    "minutes_until_deadline": minutes_until_start,
                    "urgent": now < meeting.end_at and minutes_until_start <= self.preparation_lead_minutes and not readiness["ready"],
                    "ready": readiness["ready"],
                    "missing_device_ids": [
                        item["device_id"]
                        for item in readiness["checklist"]
                        if not item["ready"]
                    ],
                }
            )

        print_requests = []
        for request_id, request in self.printing.requests.items():
            if request["status"] == "completed":
                continue
            ready_by = datetime.fromisoformat(str(request["ready_by"]))
            minutes_until_deadline = (ready_by - now).total_seconds() / 60.0
            print_requests.append(
                {
                    "commitment_type": "print_request",
                    "request_id": request_id,
                    "document_name": request["document_name"],
                    "copies": request["copies"],
                    "pages_per_copy": request["pages_per_copy"],
                    "priority": request["priority"],
                    "ready_by": ready_by.isoformat(),
                    "minutes_until_deadline": minutes_until_deadline,
                    "urgent": minutes_until_deadline <= 60.0,
                    "status": request["status"],
                    "job_id": request.get("job_id"),
                }
            )

        commitments = sorted(
            [*meetings, *print_requests],
            key=lambda item: float(item["minutes_until_deadline"]),
        )
        return {
            "status": "ok",
            "current_time": now.isoformat(),
            "commitments": commitments,
            "urgent_commitments": [item for item in commitments if item["urgent"]],
            "rule": (
                "Complete urgent print work and meeting readiness before advancing "
                "time for non-urgent environment tuning."
            ),
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_power_budget_status(self) -> dict[str, Any]:
        """Estimate configured maximum draw from current public device states.

        HVAC compressor draw varies with thermal demand. The estimate therefore
        assumes full demand at the commanded level; staying under this bound is
        a safe precondition before advancing simulated time.
        """

        active_devices = []
        estimated_power_w = 0.0
        for device_id, spec in self.world.devices.items():
            state = self.world.device_states[device_id]
            estimate = self._estimated_device_power_w(spec, state)
            if estimate <= 0.0:
                continue
            estimated_power_w += estimate
            active_devices.append(
                {
                    "device_id": device_id,
                    "room_id": spec.room_id,
                    "device_type": spec.device_type.value,
                    "level": state.level,
                    "estimated_max_power_w": estimate,
                }
            )

        latest_measured_w = None
        for item in reversed(self.runtime.trace):
            if item.get("kind") == "physics_step":
                latest_measured_w = sum(
                    float(zone.get("electric_power_w", 0.0))
                    for zone in item.get("zones", [])
                )
                break
        within_limit = (
            self.power_limit_w is None
            or estimated_power_w <= self.power_limit_w
        )
        return {
            "status": "ok",
            "power_limit_w": self.power_limit_w,
            "estimated_configured_max_power_w": estimated_power_w,
            "latest_measured_power_w": latest_measured_w,
            "headroom_w": (
                self.power_limit_w - estimated_power_w
                if self.power_limit_w is not None
                else None
            ),
            "within_limit": within_limit,
            "active_devices": sorted(
                active_devices,
                key=lambda item: float(item["estimated_max_power_w"]),
                reverse=True,
            ),
            "rule": (
                "Do not advance time while within_limit is false; reduce or "
                "stage lower-priority device levels and check again."
                if self.power_limit_w is not None
                else "No scenario-wide power cap is active."
            ),
        }

    @staticmethod
    def _estimated_device_power_w(spec: DeviceSpec, state: DeviceState) -> float:
        if not state.power_on or state.health != DeviceHealth.ONLINE:
            return 0.0
        level_fraction = max(1, min(3, state.level)) / 3.0
        if spec.device_type == DeviceType.HVAC:
            fan_power = float(
                spec.parameters.get("fan_power_w", spec.rated_power_w)
            ) * level_fraction
            if state.mode in {"cooling", "heating"}:
                capacity = float(
                    spec.parameters.get("thermal_capacity_w", 0.0)
                ) * level_fraction
                cop = 3.2 if state.mode == "cooling" else 3.0
                return fan_power + capacity / cop
            return fan_power
        if spec.device_type in {
            DeviceType.VENTILATION,
            DeviceType.HUMIDIFIER,
            DeviceType.AIR_PURIFIER,
        }:
            return spec.rated_power_w * level_fraction
        if spec.device_type == DeviceType.LIGHTING:
            return spec.rated_power_w * (
                float(state.settings.get("brightness_pct", 100.0)) / 100.0
            )
        return spec.rated_power_w

    def preflight_room_device_command(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Reject unsafe delegated changes before a room worker mutates state."""

        device_id = arguments.get("device_id")
        if not isinstance(device_id, str):
            return None
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None or "power_on" not in arguments:
            return None
        power_on = arguments["power_on"]
        if not isinstance(power_on, bool):
            return None

        now = self.runtime.current_time
        imminent_meetings = [
            meeting.meeting_id
            for meeting in self.world.schedule.values()
            if meeting.status == MeetingStatus.CONFIRMED
            and meeting.room_id == spec.room_id
            and now < meeting.end_at
            and meeting.start_at
            <= now + timedelta(minutes=self.preparation_lead_minutes)
        ]
        if self.fair_wait_enabled and now >= self.operational_end_at and power_on:
            return {"status": "rejected", "reason": "closeout_requires_power_off",
                    "device_id": device_id, "operational_end_at": self.operational_end_at.isoformat(),
                    "required_action": "The operational horizon has been reached. Finish actual shutdown instead of starting new device operation."}
        if (
            tool_name == "VentilationApp__set_ventilation"
            and not power_on
            and (
                imminent_meetings
                or self.world.room_states[spec.room_id].occupancy_count > 0
            )
        ):
            return {
                "status": "rejected",
                "reason": "meeting_ventilation_required",
                "device_id": device_id,
                "meeting_ids": imminent_meetings,
                "occupancy_count": self.world.room_states[spec.room_id].occupancy_count,
            }
        if not power_on:
            active_requirements = [
                meeting for meeting in self.world.schedule.values()
                if meeting.status == MeetingStatus.CONFIRMED
                and meeting.room_id == spec.room_id
                and meeting.start_at <= now < meeting.end_at
                and any(item["device_id"] == device_id for item in self.get_meeting_readiness(meeting.meeting_id)["checklist"])
            ]
            if active_requirements:
                return {"status": "rejected", "reason": "meeting_equipment_required",
                        "device_id": device_id, "current_time": now.isoformat(),
                        "meetings": [{"meeting_id": m.meeting_id, "end_at": m.end_at.isoformat()} for m in active_requirements],
                        "required_action": "Meeting is active. Keep required equipment on until its actual scheduled end; resetting a worker cannot bypass this policy."}
        if (
            self.max_unoccupied_energy_kwh is not None
            and power_on
            and spec.device_type != DeviceType.PRINTER
            and self.world.room_states[spec.room_id].occupancy_count == 0
            and not imminent_meetings
        ):
            return {
                "status": "rejected",
                "reason": "unoccupied_energy_risk",
                "device_id": device_id,
                "preparation_lead_minutes": self.preparation_lead_minutes,
            }

        if self.power_limit_w is None:
            return None
        projected = replace(state, settings=dict(state.settings))
        projected.power_on = power_on
        if not power_on:
            projected.level = 0
        elif tool_name == "HvacApp__set_hvac":
            projected.mode = str(arguments.get("mode", state.mode))
            projected.level = int(arguments.get("fan_level", state.level))
        elif "level" in arguments:
            projected.level = int(arguments["level"])
        if tool_name == "LightingApp__set_lighting" and "brightness_pct" in arguments:
            projected.settings["brightness_pct"] = arguments["brightness_pct"]
        current = float(self.get_power_budget_status()["estimated_configured_max_power_w"])
        forecast = (
            current
            - self._estimated_device_power_w(spec, state)
            + self._estimated_device_power_w(spec, projected)
        )
        if forecast > self.power_limit_w + 1e-6 and forecast > current + 1e-6:
            return {
                "status": "rejected",
                "reason": "power_budget_exceeded",
                "device_id": device_id,
                "estimated_configured_max_power_w": forecast,
                "power_limit_w": self.power_limit_w,
                "required_action": "Reduce or stage lower-priority loads first.",
            }
        return None

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_environment_control_status(self) -> dict[str, Any]:
        """Evaluate current public sensor readings against the operating policy."""

        latest = self.runtime.sensors.read_all(self.runtime.current_time)
        by_zone: dict[str, dict[str, float]] = {}
        for reading in latest:
            by_zone.setdefault(reading.zone_id, {})[reading.quantity.value] = float(
                reading.value
            )
        rows = []
        for zone_id, values in sorted(by_zone.items()):
            room_id = self.world.zones[zone_id].room_id
            occupancy = self.world.room_states[room_id].occupancy_count
            temperature = values.get(SensorQuantity.AIR_TEMPERATURE_C.value)
            humidity = values.get(SensorQuantity.RELATIVE_HUMIDITY_PCT.value)
            co2 = values.get(SensorQuantity.CO2_PPM.value)
            pm25 = values.get(SensorQuantity.PM25_UG_M3.value)
            warnings = []
            violations = []
            if occupancy > 0 and temperature is not None and not 21.0 <= temperature <= 25.0:
                violations.append("temperature_outside_comfort_band")
            if occupancy > 0 and humidity is not None and not 40.0 <= humidity <= 60.0:
                violations.append("humidity_outside_comfort_band")
            if occupancy > 0 and co2 is not None and co2 > self.max_co2_ppm:
                violations.append("co2_above_scenario_limit")
            if occupancy > 0 and pm25 is not None and pm25 > self.max_pm25_ug_m3:
                violations.append("pm25_above_scenario_limit")
            if (
                occupancy > 0
                and co2 is not None
                and co2 >= min(CO2_WARNING_THRESHOLD_PPM, self.max_co2_ppm)
            ):
                warnings.append("co2_control_warning")
            if (
                occupancy > 0
                and pm25 is not None
                and pm25 >= min(PM25_WARNING_THRESHOLD_UG_M3, self.max_pm25_ug_m3)
            ):
                warnings.append("pm25_control_warning")
            active_device_types = {
                spec.device_type
                for device_id, spec in self.world.devices.items()
                if spec.room_id == room_id
                and self.world.device_states[device_id].power_on
                and self.world.device_states[device_id].health == DeviceHealth.ONLINE
            }
            # A warning is actionable before it becomes a scenario-limit
            # violation.  Once the appropriate preventive control is running,
            # retain the warning as evidence but stop forcing five-minute
            # polling; the normal periodic check remains in effect.
            uncontrolled_warnings = []
            if (
                "co2_control_warning" in warnings
                and DeviceType.VENTILATION not in active_device_types
            ):
                uncontrolled_warnings.append("co2_control_warning")
            if (
                "pm25_control_warning" in warnings
                and DeviceType.AIR_PURIFIER not in active_device_types
            ):
                uncontrolled_warnings.append("pm25_control_warning")
            recommended_actions = []
            if occupancy > 0 and temperature is not None and temperature > 25.0:
                recommended_actions.append(
                    {
                        "action": "strengthen_hvac",
                        "device_type": DeviceType.HVAC.value,
                        "required_mode": "cooling",
                        "minimum_level": 1,
                        "preferred_level_if_power_allows": 3,
                        "reason": "occupied_temperature_above_band",
                    }
                )
            elif occupancy > 0 and temperature is not None and temperature < 21.0:
                recommended_actions.append(
                    {
                        "action": "strengthen_hvac",
                        "device_type": DeviceType.HVAC.value,
                        "required_mode": "heating",
                        "minimum_level": 1,
                        "preferred_level_if_power_allows": 3,
                        "reason": "occupied_temperature_below_band",
                    }
                )
            if occupancy > 0 and humidity is not None and humidity > 60.0:
                recommended_actions.append(
                    {
                        "action": "avoid_moisture_addition",
                        "reason": "occupied_humidity_above_band",
                        "guidance": (
                            "Keep humidification off; in cooling conditions use "
                            "stronger HVAC cooling and avoid more outdoor air than "
                            "CO2 control requires."
                        ),
                    }
                )
            rows.append(
                {
                    "room_id": room_id,
                    "zone_id": zone_id,
                    "occupancy_count": occupancy,
                    "readings": values,
                    "warnings": warnings,
                    "uncontrolled_warnings": uncontrolled_warnings,
                    "violations": violations,
                    "recommended_actions": recommended_actions,
                    "stable": not uncontrolled_warnings and not violations,
                }
            )
        occupied_unstable = any(
            row["occupancy_count"] > 0 and not row["stable"] for row in rows
        )
        next_event_at = self.runtime.event_queue.next_time()
        minutes_until_event = (
            max(0.0, (next_event_at - self.runtime.current_time).total_seconds() / 60.0)
            if next_event_at is not None
            else None
        )
        default_recheck = 5 if occupied_unstable else 15
        status = {
            "status": "ok",
            "policy": {
                "target_temperature_c": self.target_temperature_c,
                "occupied_temperature_band_c": [21.0, 25.0],
                "occupied_humidity_band_pct": [40.0, 60.0],
                "max_co2_ppm": self.max_co2_ppm,
                "max_pm25_ug_m3": self.max_pm25_ug_m3,
                "co2_warning_threshold_ppm": min(
                    CO2_WARNING_THRESHOLD_PPM, self.max_co2_ppm
                ),
                "pm25_warning_threshold_ug_m3": min(
                    PM25_WARNING_THRESHOLD_UG_M3, self.max_pm25_ug_m3
                ),
                "required_stable_checks_after_control": 2,
            },
            "rooms": rows,
            "occupied_environment_stable": not occupied_unstable,
            "recommended_recheck_minutes": (
                min(default_recheck, max(1, int(minutes_until_event)))
                if minutes_until_event is not None and minutes_until_event > 0
                else default_recheck
            ),
        }
        self._last_environment_check_at = self.runtime.current_time
        self._last_environment_device_event_count = self._device_event_count()
        self._last_environment_status = status
        return status

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def monitor_environment_until_stable(
        self,
        max_minutes: int = 30,
        check_interval_minutes: int = 5,
    ) -> dict[str, Any]:
        """Wait through bounded physical rechecks without repeated model turns.

        Only use after configuring occupied-room controls. Each step uses the
        ordinary guarded, event-stopping clock path. Return immediately on an
        urgent commitment, its preparation boundary, a new alert, an event, a
        failed guard, or two consecutive stable reads.
        This tool never changes devices or skips a business commitment.
        """

        if not 1 <= max_minutes <= 60 or not 1 <= check_interval_minutes <= 5:
            return {
                "status": "rejected",
                "reason": "monitor_bounds_invalid",
                "max_minutes_range": [1, 60],
                "check_interval_minutes_range": [1, 5],
            }
        if self.system is None:
            return {"status": "rejected", "reason": "system_clock_unavailable"}

        return self._monitor_environment(
            max_seconds=max_minutes * 60,
            interval_seconds=check_interval_minutes * 60,
            stop_when_stable=True,
        )

    def _monitor_environment(
        self, *, max_seconds: int, interval_seconds: int, stop_when_stable: bool,
        approved_room_goals=None,
    ) -> dict[str, Any]:
        """Shared event-aware monitor for explicit monitoring and fair raw waits."""

        environment = self.get_environment_control_status()
        elapsed_seconds = 0.0
        checks = 1
        advances = 0
        stable_checks = 1 if environment["occupied_environment_stable"] else 0
        baseline_signals = self._environment_signals(environment)
        previous_deviation = self._environment_deviation(environment)
        signature = self._monitor_control_signature()
        feedback = self._stagnant_wait_feedback
        repeated_plateaus = 0
        if (
            self.fair_wait_enabled
            and not stop_when_stable
            and feedback is not None
            and feedback[0] == signature
            and feedback[1] == baseline_signals
            and previous_deviation is not None
            and feedback[2] is not None
            and previous_deviation <= feedback[2] + 0.25
        ):
            repeated_plateaus = feedback[3]
        # First plateau still wakes the model after 10 minutes. If it elects
        # to wait again without changing controls, keep physical checks at 5
        # minutes but back off repeat plateau notices to 20, then 30 minutes.
        stagnant_limit = min(6, 2 + 2 * repeated_plateaus)
        stagnant_checks = 0
        reason = (
            "already_stable"
            if stable_checks and stop_when_stable
            else "time_budget_reached"
        )
        clock_result: dict[str, Any] | None = None
        urgent_commitments: list[dict[str, Any]] = []
        reconciled_event_ids: list[str] = []

        while (
            reason != "already_stable"
            and (not stop_when_stable or stable_checks < 2)
            and elapsed_seconds < max_seconds
        ):
            remaining_seconds = max_seconds - elapsed_seconds
            horizon_seconds = (
                self.operational_end_at - self.runtime.current_time
            ).total_seconds()
            if horizon_seconds <= 0:
                reason = "operational_horizon_reached"
                break
            urgent_commitments = self._monitor_urgent_commitments()
            if urgent_commitments:
                reason = "urgent_commitment"
                break
            preparation_seconds = self._next_preparation_boundary_seconds()
            step_seconds = min(interval_seconds, remaining_seconds, horizon_seconds)
            if preparation_seconds is not None:
                step_seconds = min(step_seconds, preparation_seconds)
            if step_seconds < 1:
                reason = "commitment_boundary"
                break
            clock_result = self.system._advance_seconds(
                seconds=int(step_seconds), stop_on_event=True
            )
            advances += 1
            if clock_result.get("status") != "ok":
                reason = "time_advance_rejected"
                break
            advanced = float(clock_result.get("advanced_seconds", 0.0))
            elapsed_seconds += advanced
            environment = self.get_environment_control_status()
            checks += 1
            if clock_result.get("interrupted"):
                routine = self._reconcile_routine_wait_events(clock_result, approved_room_goals, environment)
                if routine is None:
                    reason = "event_interruption"
                    break
                reconciled_event_ids.extend(routine)
            if advanced <= 0:
                reason = "no_time_progress"
                break
            if (
                preparation_seconds is not None
                and advanced >= preparation_seconds - 1e-3
            ):
                reason = "commitment_boundary"
                break
            new_signals = self._environment_signals(environment) - baseline_signals
            if new_signals:
                reason = "new_environment_alert"
                break
            deviation = self._environment_deviation(environment)
            if (
                deviation is not None
                and previous_deviation is not None
                and deviation > previous_deviation + 0.25
            ):
                previous_deviation = deviation
                reason = "environment_worsening"
                break
            if (
                deviation is not None
                and previous_deviation is not None
                and deviation > 0
                and deviation >= previous_deviation - 0.02
            ):
                stagnant_checks += 1
            else:
                stagnant_checks = 0
            previous_deviation = deviation
            if stagnant_checks >= stagnant_limit:
                reason = "environment_not_improving"
                break
            if environment["occupied_environment_stable"]:
                stable_checks += 1
                # Confirm stability at a second distinct physical observation.
                if stable_checks >= 2 and stop_when_stable:
                    reason = "stable"
                    break
            else:
                stable_checks = 0
            if elapsed_seconds >= max_seconds:
                reason = "time_budget_reached"

        if self.fair_wait_enabled and not stop_when_stable:
            self._stagnant_wait_feedback = (
                (signature, baseline_signals, previous_deviation, repeated_plateaus + 1)
                if reason == "environment_not_improving"
                else None
            )
        return {
            "status": (
                "ok" if reason in {"stable", "already_stable"} else "attention_required"
            ),
            "reason": reason,
            "elapsed_minutes": round(elapsed_seconds / 60, 4),
            "environment_checks": checks,
            "time_advance_calls": advances,
            "stable_checks": stable_checks,
            "stagnation_check_limit": stagnant_limit,
            "reconciled_event_ids": reconciled_event_ids,
            "deviation_score": previous_deviation,
            "urgent_commitments": urgent_commitments,
            "new_signals": sorted(
                f"{room_id}:{signal}"
                for room_id, signal in (
                    self._environment_signals(environment) - baseline_signals
                )
            ),
            "time_advance": (
                clock_result
                if reason in {"event_interruption", "time_advance_rejected"}
                else None
            ),
            "environment": environment,
        }

    def _reconcile_routine_wait_events(self, result, approved_room_goals=None, environment=None) -> list[str] | None:
        """Continue an already-approved wait only for proven idle-room facts.

        No device changes, model calls, future payloads, or unapproved extra time.
        Both modes use this predicate; multi additionally acknowledges exactly
        these event IDs and refuses continuation if other room work is pending.
        """
        if not self.fair_wait_enabled:
            return None
        interruptions = result.get("interruptions", [])
        if not interruptions or any(i.get("reason") != "building_event_requires_reconciliation" for i in interruptions):
            return None
        events = [e for item in interruptions for e in item.get("events", [])]
        if not events or any(e.get("event_type") not in {"occupancy_changed", "meeting_ended"} for e in events):
            return None
        if any(r["status"] != "completed" for r in self.printing.requests.values()):
            return None
        now = self.runtime.current_time
        for event in events:
            room_id = event.get("payload", {}).get("room_id") or event.get("subject_id")
            room = self.world.resolve_room_id(room_id)
            if room is None:
                return None
            state = self.world.room_states[room]
            if state.pending_task_ids:
                return None
            imminent = [m for m in self.world.schedule.values()
                        if m.status == MeetingStatus.CONFIRMED and m.room_id == room and now < m.end_at
                        and m.start_at <= now + timedelta(minutes=self.preparation_lead_minutes)]
            if state.occupancy_count:
                # An explicit plan+wait may keep handling routine occupancy
                # facts ONLY while its controls still match and all physical
                # readings/readiness are safe. An ordinary wait has no lease.
                goals = (approved_room_goals or {}).get(room)
                required_controls = {d.device_id for d in self.world.devices.values()
                                     if d.room_id == room and d.device_type in {DeviceType.HVAC, DeviceType.VENTILATION}}
                on_goals = {g["device_id"] for g in goals or [] if g.get("power_on") is True}
                observed = next((r for r in (environment or {}).get("rooms", []) if r["room_id"] == room), None)
                if (event["event_type"] != "occupancy_changed" or not goals
                        or not required_controls or not required_controls <= on_goals
                        or device_goal_mismatches(self.world, goals) or observed is None
                        or not observed.get("stable") or observed.get("warnings") or observed.get("violations")
                        or any(not self.get_meeting_readiness(m.meeting_id)["ready"] for m in imminent)):
                    return None
            else:
                if any(self.world.device_states[d.device_id].power_on for d in self.world.devices.values() if d.room_id == room) or imminent:
                    return None
        ids = [e.get("event_id") for e in events]
        if any(not isinstance(eid, str) or not eid for eid in ids):
            return None
        if self.routine_event_acknowledger is not None and not self.routine_event_acknowledger(set(ids)):
            return None
        self.reconciled_wait_event_count += len(ids)
        return ids

    def _monitor_control_signature(self) -> str:
        """Public control/commitment state; no hidden physics or event counters."""
        return json.dumps(
            {
                "devices": [
                    (
                        key,
                        state.power_on,
                        state.mode,
                        state.level,
                        state.target_temperature_c,
                        state.health.value,
                        state.settings,
                    )
                    for key, state in sorted(self.world.device_states.items())
                ],
                "rooms": [
                    (key, state.occupancy_count, state.pending_task_ids)
                    for key, state in sorted(self.world.room_states.items())
                ],
                "meetings": [
                    (
                        key,
                        meeting.status.value,
                        meeting.start_at.isoformat(),
                        meeting.end_at.isoformat(),
                    )
                    for key, meeting in sorted(self.world.schedule.items())
                ],
            },
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _environment_signals(status: dict[str, Any]) -> set[tuple[str, str]]:
        return {
            (str(room["room_id"]), str(signal))
            for room in status["rooms"]
            for signal in (
                *room["warnings"],
                *room["uncontrolled_warnings"],
                *room["violations"],
            )
        }

    def _environment_deviation(self, status: dict[str, Any]) -> float | None:
        """Normalize occupied-room out-of-band sensor distance for early wakeup."""

        total = 0.0
        observed = False
        for room in status["rooms"]:
            if int(room.get("occupancy_count", 0)) <= 0:
                continue
            values = room.get("readings", {})
            temperature = values.get(SensorQuantity.AIR_TEMPERATURE_C.value)
            humidity = values.get(SensorQuantity.RELATIVE_HUMIDITY_PCT.value)
            co2 = values.get(SensorQuantity.CO2_PPM.value)
            pm25 = values.get(SensorQuantity.PM25_UG_M3.value)
            if temperature is not None:
                observed = True
                total += max(21.0 - temperature, 0.0, temperature - 25.0)
            if humidity is not None:
                observed = True
                total += max(40.0 - humidity, 0.0, humidity - 60.0) / 5.0
            if co2 is not None:
                observed = True
                total += max(0.0, co2 - self.max_co2_ppm) / 100.0
            if pm25 is not None:
                observed = True
                total += max(0.0, pm25 - self.max_pm25_ug_m3) / 5.0
        return round(total, 4) if observed else None

    def _next_preparation_boundary_seconds(self) -> float | None:
        now = self.runtime.current_time
        pending = [
            (meeting.start_at - timedelta(minutes=self.preparation_lead_minutes) - now).total_seconds()
            for meeting in self.world.schedule.values()
            if meeting.status == MeetingStatus.CONFIRMED
            and meeting.start_at - timedelta(minutes=self.preparation_lead_minutes) > now
        ]
        return min(pending) if pending else None

    def _monitor_urgent_commitments(self) -> list[dict[str, Any]]:
        """Surface only currently disclosed, actionable business obligations."""

        now = self.runtime.current_time
        urgent: list[dict[str, Any]] = []
        for meeting in self.world.schedule.values():
            if meeting.status != MeetingStatus.CONFIRMED:
                continue
            if not (
                meeting.start_at - timedelta(minutes=self.preparation_lead_minutes)
                <= now < meeting.end_at
            ):
                continue
            readiness = self.get_meeting_readiness(meeting.meeting_id)
            if not readiness["ready"]:
                urgent.append(
                    {"type": "meeting_readiness", "meeting_id": meeting.meeting_id}
                )
        for request_id, request in self.printing.requests.items():
            if request["status"] == "completed":
                continue
            deadline = datetime.fromisoformat(str(request["ready_by"]))
            if not now <= deadline <= now + timedelta(minutes=60):
                continue
            job = self.printing.jobs.get(request.get("job_id"))
            if (
                job is None
                or float(job["ready_at_timestamp"])
                > (deadline + timedelta(minutes=2)).timestamp()
            ):
                urgent.append({"type": "print_request", "request_id": request_id})
        return urgent

    def time_advance_precondition(
        self, requested_target_timestamp: float | None = None
    ) -> dict[str, Any]:
        """Require readiness and reconciliation before physical time advances."""

        requested_target = datetime.fromtimestamp(
            (
                requested_target_timestamp
                if requested_target_timestamp is not None
                else self.runtime.current_time.timestamp()
            ),
            tz=self.runtime.current_time.tzinfo,
        )
        if self.fair_wait_enabled and requested_target > self.operational_end_at:
            return {
                "allowed": False,
                "code": "operational_horizon_reached",
                "operational_end_at": self.operational_end_at.isoformat(),
                "required_action": "Resolve closeout blockers; do not advance beyond the horizon.",
            }
        # A long jump must first stop at the published preparation window.
        # Otherwise the readiness guard below would force hours of premature
        # equipment operation just to cross a distant meeting start.
        if self.max_unoccupied_energy_kwh is not None:
            for meeting in self.world.schedule.values():
                if meeting.status != MeetingStatus.CONFIRMED:
                    continue
                preparation_at = meeting.start_at - timedelta(
                    minutes=self.preparation_lead_minutes
                )
                if self.runtime.current_time < preparation_at < requested_target:
                    return {
                        "allowed": False,
                        "code": "meeting_preparation_window_required",
                        "meeting_id": meeting.meeting_id,
                        "preparation_at": preparation_at.isoformat(),
                        "minutes_until_preparation": (
                            preparation_at - self.runtime.current_time
                        ).total_seconds()
                        / 60.0,
                        "required_action": (
                            "Advance only to the preparation window, then check "
                            "meeting readiness and configure the room."
                        ),
                    }
        # A meeting-start boundary is a hard business commitment.  Environment
        # tuning may legitimately change ventilation, so validate the complete
        # readiness checklist again immediately before an advance can cross
        # the start time (and while a confirmed meeting is already active).
        unreadied_meetings = []
        for meeting in self.world.schedule.values():
            if meeting.status != MeetingStatus.CONFIRMED:
                continue
            crosses_start = (
                self.runtime.current_time < meeting.start_at <= requested_target
            )
            active = meeting.start_at <= self.runtime.current_time < meeting.end_at
            if not (crosses_start or active):
                continue
            readiness = self.get_meeting_readiness(meeting.meeting_id)
            if not readiness["ready"]:
                unreadied_meetings.append(
                    {
                        "meeting_id": meeting.meeting_id,
                        "room_id": meeting.room_id,
                        "start_at": meeting.start_at.isoformat(),
                        "missing_device_ids": [
                            item["device_id"]
                            for item in readiness["checklist"]
                            if not item["ready"]
                        ],
                    }
                )
        if unreadied_meetings:
            return {
                "allowed": False,
                "code": "meeting_readiness_required",
                "meetings": unreadied_meetings,
                "required_tool": "BuildingOperationsApp.get_meeting_readiness",
            }

        # Do not cross a disclosed print deadline until the linked job is
        # scheduled to finish on time. Past deadlines are quality alerts, not
        # permanent blockers that would trap the controller after the fact.
        print_deadlines_at_risk = []
        for request_id, request in self.printing.requests.items():
            if request["status"] == "completed":
                continue
            deadline = datetime.fromisoformat(str(request["ready_by"]))
            latest_acceptable = deadline + timedelta(minutes=2)
            if not self.runtime.current_time < latest_acceptable <= requested_target:
                continue
            job = self.printing.jobs.get(request.get("job_id"))
            if job is not None and float(job["ready_at_timestamp"]) <= latest_acceptable.timestamp():
                continue
            print_deadlines_at_risk.append(
                {
                    "request_id": request_id,
                    "ready_by": deadline.isoformat(),
                    "job_id": request.get("job_id"),
                }
            )
        if print_deadlines_at_risk:
            return {
                "allowed": False,
                "code": "print_deadline_at_risk",
                "requests": print_deadlines_at_risk,
                "required_action": "Submit a correctly linked print job that completes before the deadline.",
            }

        if self.max_unoccupied_energy_kwh is not None:
            premature_devices = []
            for device_id, spec in self.world.devices.items():
                state = self.world.device_states[device_id]
                if (
                    not state.power_on
                    or spec.device_type == DeviceType.PRINTER
                    or self.world.room_states[spec.room_id].occupancy_count > 0
                ):
                    continue
                meeting_imminent = any(
                    meeting.status == MeetingStatus.CONFIRMED
                    and meeting.room_id == spec.room_id
                    and self.runtime.current_time < meeting.end_at
                    and meeting.start_at
                    <= self.runtime.current_time
                    + timedelta(minutes=self.preparation_lead_minutes)
                    for meeting in self.world.schedule.values()
                )
                if not meeting_imminent:
                    premature_devices.append(device_id)
            if premature_devices:
                return {
                    "allowed": False,
                    "code": "unoccupied_energy_risk",
                    "device_ids": premature_devices,
                    "preparation_lead_minutes": self.preparation_lead_minutes,
                    "required_action": "Switch off idle-room devices before advancing time.",
                }

        if self.power_limit_w is not None:
            power_status = self.get_power_budget_status()
            if not power_status["within_limit"]:
                return {
                    "allowed": False,
                    "code": "power_budget_exceeded",
                    "power_limit_w": self.power_limit_w,
                    "estimated_configured_max_power_w": power_status[
                        "estimated_configured_max_power_w"
                    ],
                    "required_action": "Reduce or stagger loads before advancing time.",
                }

        occupied_rooms = {
            room_id: state.occupancy_count
            for room_id, state in self.world.room_states.items()
            if state.occupancy_count > 0
        }
        if not occupied_rooms:
            return {"allowed": True}
        if (
            self._last_environment_check_at != self.runtime.current_time
            or self._last_environment_device_event_count != self._device_event_count()
            or self._last_environment_status is None
        ):
            return {
                "allowed": False,
                "code": "environment_reconciliation_required",
                "occupied_rooms": occupied_rooms,
                "required_tool": (
                    "BuildingOperationsApp.get_environment_control_status"
                ),
            }

        missing_controls = []
        for room in self._last_environment_status["rooms"]:
            if room["occupancy_count"] <= 0 or not (
                room["uncontrolled_warnings"] or room["violations"]
            ):
                continue
            room_id = room["room_id"]
            conditions = set(room["warnings"]) | set(room["violations"])
            required_types = set()
            if "temperature_outside_comfort_band" in conditions:
                temperature = room["readings"].get(
                    SensorQuantity.AIR_TEMPERATURE_C.value
                )
                required_mode = "cooling" if temperature > 25.0 else "heating"
                hvac = next(
                    (
                        (device_id, self.world.device_states[device_id])
                        for device_id, spec in self.world.devices.items()
                        if spec.room_id == room_id
                        and spec.device_type == DeviceType.HVAC
                    ),
                    None,
                )
                if hvac is None or not (
                    hvac[1].power_on
                    and hvac[1].health == DeviceHealth.ONLINE
                    and hvac[1].mode == required_mode
                    and hvac[1].level >= 1
                ):
                    missing_controls.append(
                        {
                            "room_id": room_id,
                            "device_type": DeviceType.HVAC.value,
                            "required_mode": required_mode,
                            "minimum_level": 1,
                            "reason": "occupied_temperature_outside_band",
                        }
                    )
            if "humidity_outside_comfort_band" in conditions:
                required_types.add(DeviceType.HVAC)
            if conditions & {"co2_control_warning", "co2_above_scenario_limit"}:
                required_types.add(DeviceType.VENTILATION)
            if conditions & {"pm25_control_warning", "pm25_above_scenario_limit"}:
                required_types.add(DeviceType.AIR_PURIFIER)
            for device_type in required_types:
                active = any(
                    spec.room_id == room_id
                    and spec.device_type == device_type
                    and self.world.device_states[device_id].power_on
                    for device_id, spec in self.world.devices.items()
                )
                if not active:
                    missing_controls.append(
                        {"room_id": room_id, "device_type": device_type.value}
                    )
        if missing_controls:
            return {
                "allowed": False,
                "code": "occupied_environment_uncontrolled",
                "missing_controls": missing_controls,
                "required_action": (
                    "Activate a relevant control, recheck environment status, "
                    "then retry time advance."
                ),
            }
        return {"allowed": True}

    def _device_event_count(self) -> int:
        return sum(
            event.event_type.value == "device_state_changed"
            for event in self.world.events
        )
