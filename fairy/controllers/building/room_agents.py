"""Room-scoped Agent primitives for Building World.

The canonical :class:`BuildingWorldApp` remains the single source of truth.
Room agents receive filtered views and guarded proxies around existing App
tools; they never own a private copy of world state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock, RLock
from types import SimpleNamespace
from typing import Any

from fairy.agents.agent.agent import Agent, AgentRunLimitExceeded
from fairy.agents.agent.messages import Messages
from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.device_plans import canonical_goals, execute_device_plan
from fairy.apps.building_world.operations_app import BuildingOperationsApp
from fairy.apps.building_world.types import (
    BuildingEvent,
    MeetingStatus,
    ReservationStatus,
)
from fairy.controllers.building.room_contracts import DeviceExpectation
from fairy.controllers.building.room_resolution import (
    reconciled_empty_room,
    resolve_device_contract,
)
from fairy.scenarios.workflow import Workflow
from fairy.tool_utils import AppTool, OperationType, app_tool
from fairy.types import event_registered

ROOM_AGENT_SYSTEM_PROMPT = """
You are the dedicated smart-building agent for room {room_id} ({room_name}).

Rules:
- You may observe and operate only room {room_id} and resources assigned to it.
- A fresh canonical room snapshot is supplied with each task. Use those exact IDs;
  do not fetch an identical snapshot again unless something has changed or is missing.
- Never guess or request access to another room's devices, sensors, meetings, or state.
- If a task requires another room or a shared-building decision, explain the dependency
  in your result so the supervisor can coordinate it.
- Tool acceptance confirms command state only; verify physical outcomes with available
  room-scoped observations when the task requires verification.
- Finish with a concise result that states completed actions, remaining blockers, and
  any follow-up the supervisor should perform. Keep it within 600 characters;
  do not repeat the full device inventory or narrate every tool call.
- A previous answer is not evidence. Required device postconditions and end-of-day
  shutdown are checked against canonical state before completion. If blocked, report
  the blocker honestly; do not claim an unexecuted action succeeded.
- Do not cancel a meeting as room cleanup; its scheduled end releases the booking.
- Do not switch on equipment for an empty room just to answer a comfort alert.
  Prepare for a future meeting only inside its disclosed preparation window.
- Keep ventilation and required equipment on through meeting start and until the
  room is empty. A low humidity reading alone is not a reason to disable
  ventilation immediately before or during an occupied meeting.
- Whole-building power is coordinated by the supervisor. If a command is rejected
  for power budget, report the needed capacity instead of retrying it unchanged.
""".strip()


DEFAULT_ROOM_TOOL_NAMES = frozenset(
    {
        "AirDeviceApp__set_air_purifier",
        "AirDeviceApp__set_humidifier",
        "BuildingSensorApp__read_zone_sensors",
        "DeviceRegistryApp__get_device_state",
        "DeviceRegistryApp__list_devices",
        "HvacApp__set_hvac",
        "LightingApp__set_lighting",
        "MeetingEquipmentApp__get_meeting_equipment_readiness",
        "MeetingEquipmentApp__set_audio_system",
        "MeetingEquipmentApp__set_projector",
        "OccupancyApp__get_room_occupancy",
        "PrintingApp__set_printer_power",
        "PrintingApp__submit_print_job",
        "ResourceAllocationApp__get_active_reservations",
        "ScheduleApp__get_room_schedule",
        "VentilationApp__set_ventilation",
    }
)

_ROOM_ID_FIELDS = ("room_id", "target_room_id", "new_room_id", "previous_room_id")
_RESOURCE_ID_FIELDS = (
    "zone_id",
    "functional_area_id",
    "device_id",
    "printer_id",
    "meeting_id",
    "reservation_id",
    "resource_id",
)


class RoomScopeViolation(PermissionError):
    """Raised when a proxied tool attempts to leave its assigned room."""


def resolve_event_room_ids(
    world: BuildingWorldApp, event: BuildingEvent
) -> tuple[str, ...]:
    """Resolve event ownership from explicit payload fields and stable IDs.

    Unknown/global events intentionally resolve to an empty tuple instead of being
    broadcast to every room.
    """

    candidates: list[str] = []
    payload = dict(event.payload)
    for field_name in _ROOM_ID_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str):
            candidates.append(value)
    room_ids = payload.get("room_ids")
    if isinstance(room_ids, (list, tuple, set, frozenset)):
        candidates.extend(str(value) for value in room_ids)

    identifiers: list[tuple[str, str]] = [("subject_id", event.subject_id)]
    for field_name in _RESOURCE_ID_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str):
            identifiers.append((field_name, value))

    resolved: list[str] = []
    for candidate in candidates:
        room_id = world.resolve_room_id(candidate)
        if room_id is not None and room_id not in resolved:
            resolved.append(room_id)
    for field_name, value in identifiers:
        room_id = _resolve_identifier_room(world, field_name, value)
        if room_id is not None and room_id not in resolved:
            resolved.append(room_id)
    return tuple(resolved)


class RoomContextApp(App):
    """Expose a filtered, read-only view of one room."""

    def __init__(self, world: BuildingWorldApp, room_id: str) -> None:
        canonical_room_id = world.resolve_room_id(room_id)
        if canonical_room_id is None:
            raise ValueError(f"unknown room_id {room_id!r}")
        super().__init__(name="RoomContextApp")
        self.world = world
        self.room_id = canonical_room_id

    @app_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_room_snapshot(self) -> dict[str, Any]:
        """Return current observable state for this agent's assigned room only."""

        room = self.world.rooms[self.room_id]
        room_state = self.world.room_states[self.room_id]
        zones = [
            zone for zone in self.world.zones.values() if zone.room_id == self.room_id
        ]
        devices = [
            device
            for device in self.world.devices.values()
            if device.room_id == self.room_id
        ]
        meetings = [
            meeting
            for meeting in self.world.schedule.values()
            if meeting.room_id == self.room_id
            and meeting.status == MeetingStatus.CONFIRMED
        ]
        reservations = [
            item
            for item in self.world.reservations.values()
            if item.resource_id == self.room_id
            and item.status == ReservationStatus.ACTIVE
        ]
        return {
            "room": {
                "room_id": room.room_id,
                "name": room.name,
                "capacity": room.capacity,
                "capabilities": sorted(room.capabilities),
                "room_type": room.room_type,
                "bookable": room.bookable,
            },
            "state": {
                "occupancy_count": room_state.occupancy_count,
                "pending_task_ids": list(room_state.pending_task_ids),
            },
            "zones": [
                {
                    "zone_id": zone.zone_id,
                    "name": zone.name,
                    "purpose": zone.purpose,
                }
                for zone in zones
            ],
            "functional_areas": [
                {
                    "area_id": area.area_id,
                    "name": area.name,
                    "purpose": area.purpose,
                    "zone_id": area.zone_id,
                    "capacity": area.capacity,
                }
                for area in self.world.functional_areas.values()
                if area.room_id == self.room_id
            ],
            "devices": [
                {
                    "device_id": device.device_id,
                    "device_type": device.device_type.value,
                    "zone_id": device.zone_id,
                    "health": self.world.device_states[device.device_id].health.value,
                    "power_on": self.world.device_states[device.device_id].power_on,
                    "mode": self.world.device_states[device.device_id].mode,
                    "level": self.world.device_states[device.device_id].level,
                    "target_temperature_c": self.world.device_states[
                        device.device_id
                    ].target_temperature_c,
                    "settings": dict(
                        self.world.device_states[device.device_id].settings
                    ),
                }
                for device in devices
            ],
            "meetings": [
                {
                    "meeting_id": meeting.meeting_id,
                    "title": meeting.title,
                    "start_at": meeting.start_at.isoformat(),
                    "end_at": meeting.end_at.isoformat(),
                    "expected_attendees": meeting.expected_attendees,
                    "required_capabilities": list(meeting.required_capabilities),
                }
                for meeting in meetings
            ],
            "active_reservations": [
                {
                    "reservation_id": item.reservation_id,
                    "owner_id": item.owner_id,
                    "start_at": item.start_at.isoformat(),
                    "end_at": item.end_at.isoformat(),
                }
                for item in reservations
            ],
        }

    @app_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_recent_room_events(self, limit: int = 20) -> dict[str, Any]:
        """Return recent occurred events that can be attributed to this room."""

        if not 1 <= limit <= 100:
            return {"error": "limit must be between 1 and 100"}
        matching = [
            event
            for event in self.world.events
            if self.room_id in resolve_event_room_ids(self.world, event)
        ][-limit:]
        return {
            "room_id": self.room_id,
            "events": [_event_to_dict(event) for event in matching],
            "latest_event_id": matching[-1].event_id if matching else None,
        }


class RoomScopedToolset:
    """Proxy selected App tools through a deterministic room policy."""

    def __init__(
        self,
        *,
        world: BuildingWorldApp,
        room_id: str,
        apps: Sequence[Any],
        allowed_tool_names: set[str] | frozenset[str] | None = None,
        execution_lock: Any | None = None,
    ) -> None:
        canonical_room_id = world.resolve_room_id(room_id)
        if canonical_room_id is None:
            raise ValueError(f"unknown room_id {room_id!r}")
        self.world = world
        self.room_id = canonical_room_id
        self.apps = tuple(apps)
        self.operations = next(
            (app for app in self.apps if isinstance(app, BuildingOperationsApp)),
            None,
        )
        self.execution_lock = execution_lock
        self.allowed_tool_names = (
            DEFAULT_ROOM_TOOL_NAMES
            if allowed_tool_names is None
            else frozenset(allowed_tool_names)
        )

    def get_tools(self) -> list[AppTool]:
        tools: list[AppTool] = []
        for app in self.apps:
            if not hasattr(app, "get_tools"):
                continue
            for tool in app.get_tools():
                schema_name = tool.to_open_ai()["function"]["name"]
                if schema_name not in self.allowed_tool_names:
                    continue
                tools.append(self._guarded_tool(tool))
        return tools

    def _guarded_tool(self, tool: AppTool) -> AppTool:
        def guarded(proxy: RoomScopedToolset, *args: Any, **kwargs: Any) -> Any:
            call_args = {
                item.name: value for item, value in zip(tool.args, args, strict=False)
            }
            call_args.update(kwargs)
            def invoke() -> Any:
                proxy._authorize(tool, call_args)
                if proxy.operations is not None:
                    rejection = proxy.operations.preflight_room_device_command(
                        f"{tool.app_name}__{tool.name}", call_args
                    )
                    if rejection is not None:
                        return rejection
                return tool(*args, **kwargs)

            if proxy.execution_lock is None:
                return invoke()
            # The canonical BuildingWorldApp uses shared counters, mappings and
            # an event list. Keep those short mutations atomic while room LLM
            # calls continue to run concurrently.
            with proxy.execution_lock:
                return invoke()

        return AppTool(
            class_name=tool.class_name,
            app_name=tool.app_name,
            name=tool.name,
            function_description=(
                f"Room-scoped to {self.room_id}. "
                f"{tool.function_description or ''}"
            ).strip(),
            args=list(tool.args),
            function=guarded,
            class_instance=self,
            write_operation=tool.write_operation,
        )

    def _authorize(self, tool: AppTool, arguments: Mapping[str, Any]) -> None:
        evidence: list[tuple[str, str, str]] = []
        for field_name in (*_ROOM_ID_FIELDS, *_RESOURCE_ID_FIELDS):
            value = arguments.get(field_name)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise RoomScopeViolation(
                    f"{tool.app_name}__{tool.name}: invalid {field_name}"
                )
            resolved = _resolve_identifier_room(self.world, field_name, value)
            if resolved is None:
                raise RoomScopeViolation(
                    f"{tool.app_name}__{tool.name}: cannot resolve {field_name}={value!r} "
                    "to a room"
                )
            evidence.append((field_name, value, resolved))
        if not evidence:
            raise RoomScopeViolation(
                f"{tool.app_name}__{tool.name}: no room-scoped resource argument"
            )
        foreign = [item for item in evidence if item[2] != self.room_id]
        if foreign:
            field_name, value, resolved = foreign[0]
            raise RoomScopeViolation(
                f"{tool.app_name}__{tool.name}: {field_name}={value!r} belongs to "
                f"room {resolved!r}, not assigned room {self.room_id!r}"
            )


@dataclass(frozen=True)
class RoomAgentDefinition:
    room_id: str
    agent_name: str
    system_message: str
    max_tool_calls: int = 20
    timeout_seconds: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RoomAgentSession:
    definition: RoomAgentDefinition
    enabled: bool = True
    agent: Agent | None = None
    inbox: deque[dict[str, Any]] = field(default_factory=deque)
    run_traces: deque[dict[str, Any]] = field(default_factory=deque)
    dropped_event_count: int = 0
    run_count: int = 0
    reset_count: int = 0
    run_ledger: list[dict[str, Any]] = field(default_factory=list)
    failure_count: int = 0
    tool_call_count_total: int = 0
    llm_turn_count_total: int = 0
    metric_totals: dict[str, Any] = field(
        default_factory=lambda: {
            "llm_call_count": 0,
            "tool_metric_count": 0,
            "llm_wall_time_seconds": 0.0,
            "tool_wall_time_seconds": 0.0,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "source": "not_applicable_without_llm",
            },
        }
    )
    last_status: str = "idle"
    last_answer: str | None = None
    run_lock: Any = field(default_factory=Lock, repr=False, compare=False)
    state_lock: Any = field(default_factory=RLock, repr=False, compare=False)


class RoomAgentRegistry:
    """Register and lazily execute one bounded logical Agent per room."""

    def __init__(
        self,
        *,
        world: BuildingWorldApp,
        llm_factory: Callable[[str], Any],
        building_apps: Sequence[Any] = (),
        allowed_tool_names: set[str] | frozenset[str] | None = None,
        time_manager: Any | None = None,
        default_max_tool_calls: int = 20,
        default_timeout_seconds: float | None = None,
        max_prompt_chars: int | None = None,
        max_repeated_identical_calls: int | None = None,
        max_inbox_events: int = 50,
        max_trace_runs: int = 20,
    ) -> None:
        if default_max_tool_calls <= 0:
            raise ValueError("default_max_tool_calls must be positive")
        if max_inbox_events <= 0 or max_trace_runs <= 0:
            raise ValueError("inbox and trace limits must be positive")
        self.world = world
        self.llm_factory = llm_factory
        self.building_apps = tuple(building_apps)
        self.allowed_tool_names = allowed_tool_names
        self.time_manager = time_manager or world.time_manager
        self.default_max_tool_calls = int(default_max_tool_calls)
        self.default_timeout_seconds = default_timeout_seconds
        self.max_prompt_chars = max_prompt_chars
        self.max_repeated_identical_calls = max_repeated_identical_calls
        self.max_inbox_events = int(max_inbox_events)
        self.max_trace_runs = int(max_trace_runs)
        self._sessions: dict[str, RoomAgentSession] = {}
        operations = next((app for app in self.building_apps if isinstance(app, BuildingOperationsApp)), None)
        self._world_tool_lock = operations.device_execution_lock if operations is not None else RLock()

    def register_all_rooms(self) -> None:
        for room_id in self.world.rooms:
            self.register_room(room_id)

    def register_room(
        self,
        room_id: str,
        *,
        agent_name: str | None = None,
        system_message: str | None = None,
        max_tool_calls: int | None = None,
        timeout_seconds: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RoomAgentDefinition:
        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None:
            raise ValueError(f"unknown room_id {room_id!r}")
        if canonical_room_id in self._sessions:
            raise ValueError(f"room agent {canonical_room_id!r} is already registered")
        room = self.world.rooms[canonical_room_id]
        definition = RoomAgentDefinition(
            room_id=canonical_room_id,
            agent_name=agent_name or f"room_agent_{canonical_room_id}",
            system_message=system_message
            or ROOM_AGENT_SYSTEM_PROMPT.format(
                room_id=canonical_room_id,
                room_name=room.name,
            ),
            max_tool_calls=(
                self.default_max_tool_calls
                if max_tool_calls is None
                else int(max_tool_calls)
            ),
            timeout_seconds=(
                self.default_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
            metadata=dict(metadata or {}),
        )
        if definition.max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")
        self._sessions[canonical_room_id] = RoomAgentSession(
            definition=definition,
            inbox=deque(),
            run_traces=deque(),
        )
        return definition

    def registered_room_ids(self) -> tuple[str, ...]:
        return tuple(self._sessions)

    def register_time_manager(self, time_manager: Any) -> None:
        """Align existing and future room workers with the shared simulation clock."""

        self.time_manager = time_manager
        for app in self.building_apps:
            if hasattr(app, "register_time_manager"):
                app.register_time_manager(time_manager)
        for session in self._sessions.values():
            if session.agent is None:
                continue
            session.agent.set_time_manager(time_manager)
            for tool in session.agent.tools or []:
                owner = getattr(tool, "class_instance", None)
                if hasattr(owner, "register_time_manager"):
                    owner.register_time_manager(time_manager)

    def list_agents(self) -> list[dict[str, Any]]:
        return [self.status(room_id) for room_id in self._sessions]

    def pending_room_ids(self) -> tuple[str, ...]:
        """Return registered rooms with unacknowledged wake-worthy events."""

        pending: list[str] = []
        for room_id, session in self._sessions.items():
            with session.state_lock:
                if session.inbox:
                    pending.append(room_id)
        return tuple(pending)

    def status(self, room_id: str) -> dict[str, Any]:
        session = self._session(room_id)
        with session.state_lock:
            return {
                "room_id": session.definition.room_id,
                "agent_name": session.definition.agent_name,
                "enabled": session.enabled,
                "initialized": session.agent is not None,
                "status": session.last_status,
                "run_count": session.run_count,
                "reset_count": session.reset_count,
                "failure_count": session.failure_count,
                "tool_call_count_total": session.tool_call_count_total,
                "llm_turn_count_total": session.llm_turn_count_total,
                "pending_event_count": len(session.inbox),
                "dropped_event_count": session.dropped_event_count,
                "last_answer": session.last_answer,
                "answer_is_proof": False,
                "current_state": self._compact_evidence(
                    self._room_evidence(session.definition.room_id)
                ),
                "max_tool_calls": session.definition.max_tool_calls,
                "timeout_seconds": session.definition.timeout_seconds,
                "metadata": dict(session.definition.metadata),
            }

    def pending_events(self, room_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= self.max_inbox_events:
            raise ValueError(
                f"limit must be between 1 and {self.max_inbox_events}"
            )
        session = self._session(room_id)
        with session.state_lock:
            return [dict(item) for item in list(session.inbox)[-limit:]]

    def set_enabled(self, room_id: str, enabled: bool) -> dict[str, Any]:
        session = self._session(room_id)
        with session.run_lock, session.state_lock:
            session.enabled = bool(enabled)
            session.last_status = "idle" if session.enabled else "disabled"
        return self.status(session.definition.room_id)

    def reset(self, room_id: str, *, clear_inbox: bool = True) -> dict[str, Any]:
        """Reset model memory, never erase historical costs or attempt records."""
        session = self._session(room_id)
        with session.run_lock, session.state_lock:
            session.agent = None
            session.reset_count += 1
            session.last_status = "idle" if session.enabled else "disabled"
            session.last_answer = None
            if clear_inbox:
                session.inbox.clear()
                session.dropped_event_count = 0
        return self.status(session.definition.room_id)

    def enqueue_event(
        self,
        room_id: str,
        event: BuildingEvent,
        *,
        should_wake_agent: bool,
        trigger_reason: str,
    ) -> dict[str, Any]:
        session = self._session(room_id)
        envelope = {
            **_event_to_dict(event),
            "should_wake_agent": bool(should_wake_agent),
            "trigger_reason": str(trigger_reason),
        }
        with session.state_lock:
            if len(session.inbox) >= self.max_inbox_events:
                session.inbox.popleft()
                session.dropped_event_count += 1
            session.inbox.append(envelope)
        return dict(envelope)

    def delegate(
        self,
        room_id: str,
        task: str,
        expected_devices: list[DeviceExpectation] | None = None,
        *,
        reconcile_events_only: bool = False,
    ) -> dict[str, Any]:
        try:
            session = self._session(room_id)
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc), "room_id": room_id}
        rejection = self.validate_expectations(
            session.definition.room_id, expected_devices
        )
        if rejection is not None:
            return rejection
        expected_devices = canonical_goals(self.world, session.definition.room_id, expected_devices)
        # One logical Agent owns mutable messages/workflow state. Serialize
        # calls to the same room while allowing different rooms to run in
        # separate worker threads.
        with session.run_lock:
            return self._delegate_session(
                session, str(task), expected_devices, reconcile_events_only=reconcile_events_only
            )

    def validate_expectations(
        self,
        room_id: str,
        expected_devices: list[DeviceExpectation] | None,
    ) -> dict[str, Any] | None:
        """Reject malformed/cross-room contracts before launching any worker."""

        try:
            canonical_goals(self.world, room_id, expected_devices)
        except ValueError as exc:
            return {
                "status": "rejected",
                "reason": "invalid_device_expectations",
                "room_id": room_id,
                "details": str(exc),
            }
        return None

    def _room_evidence(self, room_id: str) -> dict[str, Any]:
        with self._world_tool_lock:
            snapshot = RoomContextApp(self.world, room_id).get_room_snapshot()
            return {
                "occupancy_count": snapshot["state"]["occupancy_count"],
                "devices": [
                    {
                        key: device[key]
                        for key in (
                            "device_id",
                            "power_on",
                            "mode",
                            "level",
                            "target_temperature_c",
                        )
                    }
                    for device in snapshot["devices"]
                ],
            }

    @staticmethod
    def _compact_evidence(
        evidence: dict[str, Any],
        expected_devices: list[DeviceExpectation] | None = None,
    ) -> dict[str, Any]:
        expected_ids = {item["device_id"] for item in expected_devices or []}
        return {
            "occupancy_count": evidence["occupancy_count"],
            "observed_device_count": len(evidence["devices"]),
            "powered_device_ids": [
                item["device_id"] for item in evidence["devices"] if item["power_on"]
            ],
            "device_states": [
                item
                for item in evidence["devices"]
                if item["power_on"] or item["device_id"] in expected_ids
            ],
        }

    def _verify_completion(
        self,
        room_id: str,
        expected_devices: list[DeviceExpectation] | None,
    ) -> dict[str, Any]:
        evidence = self._room_evidence(room_id)
        states = {device["device_id"]: device for device in evidence["devices"]}
        blockers = []
        for expected in expected_devices or []:
            actual = states[expected["device_id"]]
            for field_name, value in expected.items():
                if field_name != "device_id" and actual[field_name] != value:
                    blockers.append(
                        {
                            "code": "device_postcondition_failed",
                            "device_id": expected["device_id"],
                            "field": field_name,
                            "expected": value,
                            "actual": actual[field_name],
                        }
                    )
        operations = next(
            (
                app
                for app in self.building_apps
                if isinstance(app, BuildingOperationsApp)
            ),
            None,
        )
        closeout = (
            operations is not None
            and operations.runtime.current_time >= operations.operational_end_at
        )
        if closeout:
            if evidence["occupancy_count"]:
                blockers.append({"code": "room_still_occupied", "room_id": room_id})
            powered = [
                device_id for device_id, state in states.items() if state["power_on"]
            ]
            if powered:
                blockers.append(
                    {"code": "room_devices_still_on", "device_ids": powered}
                )
        return {
            "can_finish": not blockers,
            "scope": (
                "requested_device_fields_and_closeout"
                if expected_devices or closeout
                else "state_observed_only; natural-language task not verified"
            ),
            "blockers": blockers,
            "evidence": self._compact_evidence(evidence, expected_devices),
            "required_action": "Use room-scoped device tools to resolve the actual mismatches. Do not repeat an unsupported completion claim.",
        }

    async def delegate_async(
        self,
        room_id: str,
        task: str,
        expected_devices: list[DeviceExpectation] | None = None,
    ) -> dict[str, Any]:
        """Run one room Agent without blocking the caller's event loop."""

        return await asyncio.to_thread(self.delegate, room_id, task, expected_devices)

    def _task_resolution(
        self, room_id, expected_devices, *, pending_events, reconcile_events_only,
        agent=None, max_tool_calls=None, timeout_seconds=None,
    ):
        with self._world_tool_lock:
            operations = next(
                (
                    app
                    for app in self.building_apps
                    if isinstance(app, BuildingOperationsApp)
                ),
                None,
            )
            if expected_devices:
                resolution = resolve_device_contract(
                    self.world,
                    operations,
                    room_id,
                    expected_devices,
                    self._verify_completion(room_id, expected_devices),
                )
                if resolution is not None or operations is None or not operations.fair_wait_enabled or agent is None:
                    return resolution

                def invoke(name, arguments):
                    if name not in agent.tools_map:
                        return {"status": "rejected", "reason": "device_tool_not_authorized"}
                    return agent.call_function(SimpleNamespace(
                        type="function", id=f"controller-plan-{agent.tool_call_count + 1}",
                        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
                    ))

                result = execute_device_plan(
                    operations, room_id, expected_devices, invoke,
                    before_action=lambda: agent._check_limits(max_tool_calls=max_tool_calls, timeout_seconds=timeout_seconds),
                    actor="room_controller",
                )
                if result["reason"] in {"device_plan_requires_model", "device_tool_unavailable"}:
                    return None
                # Controller-produced tool observations have no model-authored
                # assistant/tool-call prefix. Never fall through to an LLM
                # after partial closeout; return actual blockers instead.
                verification = self._verify_completion(room_id, expected_devices)
                if result["status"] == "ok" and not verification["can_finish"]:
                    return {"status": "blocked", "reason": "room_closeout_incomplete",
                            "execution": "guarded_device_plan",
                            "action_count": len(result.get("actions", [])),
                            "blockers": verification["blockers"],
                            "answer": "Declared device fields match, but room closeout is incomplete. Resolve the remaining canonical blockers."}
                return {"status": "ok" if result["status"] == "ok" else "blocked",
                        "reason": result["reason"], "execution": "guarded_device_plan",
                        "action_count": len(result.get("actions", [])),
                        "blockers": result.get("blockers", []),
                        "answer": "Explicit device plan executed and verified against canonical state." if result["status"] == "ok" else "Device plan stopped; inspect the structured blockers and actual state before retrying."}
            if reconcile_events_only and reconciled_empty_room(
                self.world, operations, room_id, pending_events
            ):
                return {
                    "status": "ok",
                    "reason": "empty_room_events_already_reconciled",
                    "answer": "Room is empty, all devices are off, and no imminent room or print work requires action. Supplied end/occupancy events verified.",
                }
        return None

    def delegate_many(
        self,
        room_ids: Sequence[str],
        task: str,
        *,
        max_concurrency: int,
        reconcile_events_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Run different room Agents concurrently from synchronous code."""

        return self.delegate_tasks(
            [(room_id, task) for room_id in room_ids],
            max_concurrency=max_concurrency,
            reconcile_events_only=reconcile_events_only,
        )

    def delegate_tasks(
        self,
        room_tasks: Sequence[
            tuple[str, str] | tuple[str, str, list[DeviceExpectation] | None]
        ],
        *,
        max_concurrency: int,
        reconcile_events_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Run different room tasks concurrently, preserving input order."""

        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        ordered_tasks = list(room_tasks)
        if not ordered_tasks:
            return []
        worker_count = min(int(max_concurrency), len(ordered_tasks))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="fairy-room-agent",
        ) as executor:
            futures = [
                executor.submit(
                    self.delegate, *item, reconcile_events_only=reconcile_events_only
                )
                for item in ordered_tasks
            ]
            return [future.result() for future in futures]

    async def delegate_many_async(
        self,
        room_ids: Sequence[str],
        task: str,
        *,
        max_concurrency: int,
    ) -> list[dict[str, Any]]:
        """Run different room Agents concurrently with bounded async fan-out."""

        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        semaphore = asyncio.Semaphore(int(max_concurrency))

        async def run_one(room_id: str) -> dict[str, Any]:
            async with semaphore:
                return await self.delegate_async(room_id, task)

        return list(await asyncio.gather(*(run_one(room_id) for room_id in room_ids)))

    def _delegate_session(
        self,
        session: RoomAgentSession,
        task: str,
        expected_devices: list[DeviceExpectation] | None = None,
        *,
        reconcile_events_only: bool = False,
    ) -> dict[str, Any]:
        with session.state_lock:
            if not session.enabled:
                return {
                    "status": "rejected",
                    "reason": "room_agent_disabled",
                    "room_id": session.definition.room_id,
                }
            if not task.strip():
                return {
                    "status": "rejected",
                    "reason": "task_cannot_be_empty",
                    "room_id": session.definition.room_id,
                }
            pending_events = list(session.inbox)
            event_ids = [str(item["event_id"]) for item in pending_events]
            session.last_status = "running"

        try:
            agent = session.agent or self._build_agent(session)
            self._reset_agent_for_run(agent, session.definition.system_message)
            agent.completion_guard = lambda: self._verify_completion(
                session.definition.room_id, expected_devices
            )
            agent.max_completion_guard_rejections = 3
            agent.run_resolution_guard = lambda: self._task_resolution(
                session.definition.room_id, expected_devices,
                pending_events=pending_events, reconcile_events_only=reconcile_events_only,
                agent=agent, max_tool_calls=session.definition.max_tool_calls,
                timeout_seconds=session.definition.timeout_seconds,
            )
            with self._world_tool_lock:
                snapshot = RoomContextApp(self.world, session.definition.room_id).get_room_snapshot()
            prompt = self._delegated_prompt(
                session=session,
                task=task,
                pending_events=pending_events,
                snapshot=snapshot,
                expected_devices=expected_devices,
                initial_blockers=self._verify_completion(
                    session.definition.room_id, expected_devices
                )["blockers"],
            )
            answer = agent.run(
                prompt,
                max_tool_calls=session.definition.max_tool_calls,
                timeout_seconds=session.definition.timeout_seconds,
            )
            with session.state_lock:
                session.run_count += 1
                resolution = agent.run_resolution or {"status": "ok"}
                session.last_status = resolution["status"]
                session.last_answer = str(answer or "")[:600]
                if session.last_status == "ok":
                    self._acknowledge_events(session, len(pending_events))
                result = self._run_result(
                    session=session,
                    agent=agent,
                    status=session.last_status,
                    task=task,
                    answer=str(answer or ""),
                    event_ids=event_ids,
                    reason=resolution.get("reason"),
                )
                if agent.run_resolution is not None:
                    result["resolution"] = dict(agent.run_resolution)
        except AgentRunLimitExceeded as exc:
            with session.state_lock:
                session.run_count += 1
                session.failure_count += 1
                session.last_status = "stopped"
                session.last_answer = (
                    f"Room agent stopped at {exc.limit_type}={exc.limit_value}; "
                    "pending events were retained for retry."
                )
                result = self._run_result(
                    session=session,
                    agent=session.agent,
                    status="stopped",
                    task=task,
                    answer=session.last_answer,
                    event_ids=event_ids,
                    reason=f"{exc.limit_type}={exc.limit_value}",
                )
        # A room worker may fail for any provider/tool reason without taking
        # the supervisor or another room agent down with it.
        except Exception as exc:  # noqa: BLE001
            with session.state_lock:
                session.run_count += 1
                session.failure_count += 1
                session.last_status = "error"
                session.last_answer = None
                result = self._run_result(
                    session=session,
                    agent=session.agent,
                    status="error",
                    task=task,
                    answer=None,
                    event_ids=event_ids,
                    reason=f"{type(exc).__name__}: {exc}",
                )
        result["verification"] = self._verify_completion(session.definition.room_id, expected_devices)
        run_metrics = _empty_metric_totals()
        _accumulate_runtime_metrics(run_metrics, result["trace"]["runtime_metrics"])
        result["run_id"] = session.run_count
        result["generation"] = session.reset_count
        result["token_usage"] = dict(run_metrics["token_usage"])
        if result.get("resolution") is not None:
            # Run IDs/reset generations/usage are audit metadata, not progress.
            material = {"status": result["status"], "reason": result["reason"],
                        "verification": result["verification"],
                        "retry_at": result["resolution"].get("retry_at")}
            result["progress_key"] = hashlib.sha256(
                json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        with session.state_lock:
            self._accumulate_run_totals(session, result)
            self._append_trace(session, result)
        public_result = {key: value for key, value in result.items() if key != "trace"}
        if public_result.get("answer"):
            public_result["answer"] = public_result["answer"][:600]
        return public_result

    def traces(self, room_id: str) -> list[dict[str, Any]]:
        session = self._session(room_id)
        with session.state_lock:
            return list(session.run_traces)

    def all_traces(self) -> dict[str, list[dict[str, Any]]]:
        """Return bounded run traces grouped by room for reporting."""

        return {room_id: self.traces(room_id) for room_id in self._sessions}

    def all_run_ledgers(self) -> dict[str, list[dict[str, Any]]]:
        """Small complete attempt summaries, independent of bounded full traces."""
        ledgers = {}
        for room_id, session in self._sessions.items():
            with session.state_lock:
                ledgers[room_id] = _json_safe(session.run_ledger)
        return ledgers

    def runtime_metrics(self) -> list[dict[str, Any]]:
        """Flatten retained room metrics for bounded diagnostic artifacts."""

        metrics: list[dict[str, Any]] = []
        for room_id in self._sessions:
            for run in self.traces(room_id):
                trace = run.get("trace", {})
                if isinstance(trace, Mapping):
                    raw_metrics = trace.get("runtime_metrics", [])
                    if isinstance(raw_metrics, list):
                        metrics.extend(
                            dict(item)
                            for item in raw_metrics
                            if isinstance(item, Mapping)
                        )
        return metrics

    def cumulative_metric_totals(self) -> dict[str, Any]:
        """Return exact cumulative metrics independent of trace retention."""

        totals = _empty_metric_totals()
        for session in self._sessions.values():
            with session.state_lock:
                _merge_metric_totals(totals, session.metric_totals)
        return totals

    def snapshot(self) -> dict[str, Any]:
        """Serialize durable room state without model clients or thread locks."""

        sessions: dict[str, dict[str, Any]] = {}
        for room_id, session in self._sessions.items():
            with session.run_lock, session.state_lock:
                definition = session.definition
                sessions[room_id] = {
                    "definition": {
                        "room_id": definition.room_id,
                        "agent_name": definition.agent_name,
                        "system_message": definition.system_message,
                        "max_tool_calls": definition.max_tool_calls,
                        "timeout_seconds": definition.timeout_seconds,
                        "metadata": _json_safe(definition.metadata),
                    },
                    "enabled": session.enabled,
                    "inbox": _json_safe(list(session.inbox)),
                    "run_traces": _json_safe(list(session.run_traces)),
                    "dropped_event_count": session.dropped_event_count,
                    "run_count": session.run_count,
                    "reset_count": session.reset_count,
                    "run_ledger": _json_safe(session.run_ledger),
                    "failure_count": session.failure_count,
                    "tool_call_count_total": session.tool_call_count_total,
                    "llm_turn_count_total": session.llm_turn_count_total,
                    "metric_totals": _json_safe(session.metric_totals),
                    "last_status": session.last_status,
                    "last_answer": session.last_answer,
                }
        return {"schema_version": 1, "sessions": sessions}

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Replace durable room state and lazily rebuild model sessions."""

        if int(snapshot.get("schema_version", 0)) != 1:
            raise ValueError("unsupported room-agent snapshot schema_version")
        raw_sessions = snapshot.get("sessions")
        if not isinstance(raw_sessions, Mapping):
            raise TypeError("room-agent snapshot must contain sessions")
        snapshot_room_ids = {str(room_id) for room_id in raw_sessions}
        registered_room_ids = set(self._sessions)
        if snapshot_room_ids != registered_room_ids:
            raise ValueError(
                "room-agent snapshot room IDs do not match the current registry"
            )

        for room_id, session in self._sessions.items():
            raw_session = raw_sessions[room_id]
            if not isinstance(raw_session, Mapping):
                raise TypeError(f"invalid room-agent snapshot for {room_id!r}")
            raw_definition = raw_session.get("definition", {})
            if not isinstance(raw_definition, Mapping):
                raise TypeError(f"invalid room-agent definition for {room_id!r}")
            if str(raw_definition.get("room_id")) != room_id:
                raise ValueError(f"room-agent definition mismatch for {room_id!r}")

            with session.run_lock, session.state_lock:
                session.definition = RoomAgentDefinition(
                    room_id=room_id,
                    agent_name=str(raw_definition.get("agent_name") or ""),
                    system_message=str(raw_definition.get("system_message") or ""),
                    max_tool_calls=int(raw_definition.get("max_tool_calls", 20)),
                    timeout_seconds=(
                        float(raw_definition["timeout_seconds"])
                        if raw_definition.get("timeout_seconds") is not None
                        else None
                    ),
                    metadata=dict(raw_definition.get("metadata", {})),
                )
                if session.definition.max_tool_calls <= 0:
                    raise ValueError("snapshot max_tool_calls must be positive")
                session.enabled = bool(raw_session.get("enabled", True))
                session.agent = None
                session.inbox = deque(
                    dict(item)
                    for item in list(raw_session.get("inbox", []))[
                        -self.max_inbox_events :
                    ]
                )
                session.run_traces = deque(
                    dict(item)
                    for item in list(raw_session.get("run_traces", []))[
                        -self.max_trace_runs :
                    ]
                )
                session.dropped_event_count = int(
                    raw_session.get("dropped_event_count", 0)
                )
                session.run_count = int(raw_session.get("run_count", 0))
                session.reset_count = int(raw_session.get("reset_count", 0))
                session.run_ledger = [dict(item) for item in raw_session.get("run_ledger", [])]
                session.failure_count = int(raw_session.get("failure_count", 0))
                session.tool_call_count_total = int(
                    raw_session.get("tool_call_count_total", 0)
                )
                session.llm_turn_count_total = int(
                    raw_session.get("llm_turn_count_total", 0)
                )
                session.metric_totals = _normalized_metric_totals(
                    raw_session.get("metric_totals")
                )
                restored_status = str(raw_session.get("last_status", "idle"))
                session.last_status = (
                    "interrupted" if restored_status == "running" else restored_status
                )
                session.last_answer = (
                    str(raw_session["last_answer"])
                    if raw_session.get("last_answer") is not None
                    else None
                )

    def _session(self, room_id: str) -> RoomAgentSession:
        canonical_room_id = self.world.resolve_room_id(room_id)
        if canonical_room_id is None or canonical_room_id not in self._sessions:
            raise ValueError(f"unknown or unregistered room_id {room_id!r}")
        return self._sessions[canonical_room_id]

    def _build_agent(self, session: RoomAgentSession) -> Agent:
        room_id = session.definition.room_id
        context_app = RoomContextApp(self.world, room_id)
        scoped_tools = RoomScopedToolset(
            world=self.world,
            room_id=room_id,
            apps=self.building_apps,
            allowed_tool_names=self.allowed_tool_names,
            execution_lock=self._world_tool_lock,
        )
        llm = self.llm_factory(room_id)
        agent = Agent(
            name=session.definition.agent_name,
            llm=llm,
            system_message=session.definition.system_message,
            toolsets=[context_app, scoped_tools],
        )
        agent.max_prompt_chars = self.max_prompt_chars
        agent.max_repeated_identical_calls = self.max_repeated_identical_calls
        context_app.register_time_manager(self.time_manager)
        agent.set_time_manager(self.time_manager)
        with session.state_lock:
            session.agent = agent
        return agent

    @staticmethod
    def _reset_agent_for_run(agent: Agent, system_message: str) -> None:
        agent.messages = Messages(
            provider=getattr(agent.llm, "provider", "openai"),
            system_message=system_message,
        )
        agent.workflow = Workflow()
        agent.runtime_events = []
        agent.tool_call_count = 0
        agent.llm_turn_count = 0

    @staticmethod
    def _delegated_prompt(
        *,
        session: RoomAgentSession,
        task: str,
        pending_events: list[dict[str, Any]],
        snapshot: dict[str, Any],
        expected_devices: list[DeviceExpectation] | None,
        initial_blockers: list[dict[str, Any]],
    ) -> str:
        return (
            f"Assigned room: {session.definition.room_id}\n"
            f"Supervisor task: {task}\n"
            "Fresh canonical room state (supersedes prior answers; no initial snapshot call needed):\n"
            f"{json.dumps(snapshot, ensure_ascii=False)}\n"
            f"Required device postconditions: {json.dumps(expected_devices or [], ensure_ascii=False)}\n"
            f"Current completion mismatches (including end-of-day shutdown): {json.dumps(initial_blockers, ensure_ascii=False)}\n"
            "Pending room events (occurred facts only):\n"
            f"{json.dumps(pending_events, ensure_ascii=False)}"
        )

    @staticmethod
    def _acknowledge_events(session: RoomAgentSession, count: int) -> None:
        for _ in range(min(count, len(session.inbox))):
            session.inbox.popleft()

    @staticmethod
    def _run_result(
        *,
        session: RoomAgentSession,
        agent: Agent | None,
        status: str,
        task: str,
        answer: str | None,
        event_ids: list[str],
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "room_id": session.definition.room_id,
            "agent_name": session.definition.agent_name,
            "answer": answer,
            "reason": reason,
            "processed_event_ids": event_ids if status == "ok" else [],
            "pending_event_count": len(session.inbox),
            "tool_call_count": int(getattr(agent, "tool_call_count", 0)),
            "llm_turn_count": int(getattr(agent, "llm_turn_count", 0)),
            "trace": {
                "task": task,
                "event_ids": event_ids,
                "runtime_events": list(getattr(agent, "runtime_events", [])),
                "runtime_metrics": list(
                    getattr(getattr(agent, "messages", None), "runtime_metrics", [])
                ),
                "messages": list(getattr(getattr(agent, "messages", None), "messages", [])),
                "workflow": (
                    agent.workflow.to_dict() if agent is not None else {}
                ),
            },
        }

    def _append_trace(
        self, session: RoomAgentSession, result: Mapping[str, Any]
    ) -> None:
        if len(session.run_traces) >= self.max_trace_runs:
            session.run_traces.popleft()
        session.run_traces.append(dict(result))
        totals = _empty_metric_totals()
        _accumulate_runtime_metrics(totals, result.get("trace", {}).get("runtime_metrics", []))
        session.run_ledger.append({
            "run_id": session.run_count, "generation": session.reset_count,
            "status": result.get("status"), "reason": result.get("reason"),
            "tool_call_count": result.get("tool_call_count", 0),
            "llm_turn_count": result.get("llm_turn_count", 0),
            "execution": result.get("resolution", {}).get("execution"),
            "device_plan_action_count": result.get("resolution", {}).get("action_count", 0),
            "metric_totals": totals,
        })

    @staticmethod
    def _accumulate_run_totals(
        session: RoomAgentSession,
        result: Mapping[str, Any],
    ) -> None:
        session.tool_call_count_total += int(result.get("tool_call_count", 0))
        session.llm_turn_count_total += int(result.get("llm_turn_count", 0))
        trace = result.get("trace", {})
        metrics = trace.get("runtime_metrics", []) if isinstance(trace, Mapping) else []
        _accumulate_runtime_metrics(
            session.metric_totals,
            metrics if isinstance(metrics, list) else [],
        )


def _resolve_identifier_room(
    world: BuildingWorldApp, field_name: str, value: str
) -> str | None:
    if field_name in _ROOM_ID_FIELDS or field_name == "subject_id":
        direct = world.resolve_room_id(value)
        if direct is not None:
            return direct
    zone = world.zones.get(value)
    if zone is not None:
        return zone.room_id
    area = world.functional_areas.get(value)
    if area is not None:
        return area.room_id
    device = world.devices.get(value)
    if device is not None:
        return device.room_id
    meeting = world.schedule.get(value)
    if meeting is not None:
        return meeting.room_id
    reservation = world.reservations.get(value)
    if reservation is not None:
        resource_room = world.resolve_room_id(reservation.resource_id)
        if resource_room is not None:
            return resource_room
        resource_device = world.devices.get(reservation.resource_id)
        if resource_device is not None:
            return resource_device.room_id
    if field_name == "resource_id":
        resource_room = world.resolve_room_id(value)
        if resource_room is not None:
            return resource_room
    return None


def _event_to_dict(event: BuildingEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(),
        "source": event.source,
        "subject_id": event.subject_id,
        "payload": dict(event.payload),
        "parent_event_id": event.parent_event_id,
    }


def _json_safe(value: Any) -> Any:
    """Return a detached JSON-compatible copy for checkpoints and artifacts."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _empty_metric_totals() -> dict[str, Any]:
    return {
        "llm_call_count": 0,
        "tool_metric_count": 0,
        "llm_wall_time_seconds": 0.0,
        "tool_wall_time_seconds": 0.0,
        "token_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "source": "not_applicable_without_llm",
        },
    }


def _accumulate_runtime_metrics(
    totals: dict[str, Any],
    metrics: Sequence[Mapping[str, Any]],
) -> None:
    token_usage = totals["token_usage"]
    for entry in metrics:
        role = entry.get("role")
        if role == "assistant":
            totals["llm_call_count"] += 1
            totals["llm_wall_time_seconds"] += float(entry.get("time") or 0)
        elif role == "tool":
            totals["tool_metric_count"] += 1
            totals["tool_wall_time_seconds"] += float(entry.get("time") or 0)
        tokens = entry.get("tokens") or {}
        if not isinstance(tokens, Mapping):
            continue
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
        ):
            token_usage[key] += int(tokens.get(key) or 0)
    totals["llm_wall_time_seconds"] = round(
        totals["llm_wall_time_seconds"], 6
    )
    totals["tool_wall_time_seconds"] = round(
        totals["tool_wall_time_seconds"], 6
    )
    if token_usage["total_tokens"]:
        token_usage["source"] = "llm_runtime_metrics"


def _merge_metric_totals(
    target: dict[str, Any], source: Mapping[str, Any]
) -> None:
    for key in ("llm_call_count", "tool_metric_count"):
        target[key] += int(source.get(key) or 0)
    for key in ("llm_wall_time_seconds", "tool_wall_time_seconds"):
        target[key] = round(
            target[key] + float(source.get(key) or 0),
            6,
        )
    target_tokens = target["token_usage"]
    source_tokens = source.get("token_usage") or {}
    if isinstance(source_tokens, Mapping):
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
        ):
            target_tokens[key] += int(source_tokens.get(key) or 0)
    if target_tokens["total_tokens"]:
        target_tokens["source"] = "llm_runtime_metrics"


def _normalized_metric_totals(value: Any) -> dict[str, Any]:
    totals = _empty_metric_totals()
    if isinstance(value, Mapping):
        _merge_metric_totals(totals, value)
    return totals
