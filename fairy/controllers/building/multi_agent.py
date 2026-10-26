"""Supervisor, routing and assembly for Building World multi-agent control."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any

from fairy.agents.agent.agent import Agent, AgentRunLimitExceeded
from fairy.agents.agent.messages import Messages
from fairy.apps.app import App
from fairy.apps.building_world.building_world_app import BuildingWorldApp
from fairy.apps.building_world.device_plans import canonical_goals, coalesce_room_tasks
from fairy.apps.building_world.operations_app import BuildingOperationsApp
from fairy.apps.building_world.runtime import BuildingWorldRuntime
from fairy.apps.building_world.types import BuildingEvent, BuildingEventType
from fairy.controllers.agent_config import BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
from fairy.controllers.building.room_agents import (
    RoomAgentRegistry,
    resolve_event_room_ids,
)
from fairy.controllers.building.room_contracts import DeviceExpectation, RoomTask
from fairy.controllers.building.trigger_policy import TriggerDecision
from fairy.physics.building.sensor_api import (
    SensorQuality,
    SensorQuantity,
    SensorReading,
)
from fairy.scenarios.workflow import Workflow
from fairy.tool_utils import OperationType, app_tool
from fairy.types import event_registered

SUPERVISOR_COORDINATION_PROMPT = """
You are the building supervisor agent. Room agents are independent bounded workers.
Use RoomAgentManagerApp to inspect their status and delegate room-local work. Keep
cross-room scheduling, shared-resource decisions, prioritization, and conflict
resolution at the supervisor level. A worker answer is not proof: check its
structured verification and canonical device evidence. Provide expected_devices
for device tasks; status=ok without a contract verifies execution, not arbitrary
natural-language goals. If one room agent fails, continue safe work in
other rooms and report the isolated failure.
 Never cancel a scheduled meeting merely to clear an end-of-day blocker: the
 normal meeting-end event completes it and releases its reservation. Treat
 cancellation as a business decision only when explicitly requested.
 Prioritize imminent meeting-readiness and print deadlines before comfort tuning.
 Avoid repeated broad building-overview reads when targeted status tools suffice.
  Before delegating an increase in electrical load, inspect whole-building power
  headroom and specify a feasible room budget. Do not delegate switching off
  ventilation or required equipment for a meeting that is imminent or active.
  For strict idle-energy scenarios, prepare rooms only within the published
  preparation window and shut down empty rooms promptly.
  For independent room-local tasks with no unresolved shared-power or schedule
  dependency, prefer one bounded RoomAgentManagerApp.delegate_room_tasks call
  over sequential delegate_to_room calls. Recheck global power and commitments
  after the batch. Keep dependent tasks sequential.
  Combine all presently known actions for the SAME room into one coherent task
  (HVAC, ventilation, meeting equipment, and verification), not one delegation
  per device. State desired modes/levels, room budget, and expected_devices.
  Do not poll completed worker status: delegation returns its evidence directly.
  A blocked result requires a changed plan or shared-resource decision. A deferred
  result gives retry_at when known: wait to that boundary or handle other work.
  Do not repeat or reset a worker to bypass the same policy blocker. Verified
  device contracts may finish without any LLM call or prose summary.
  Explicit feasible device contracts execute through a guarded deterministic plan;
  use one complete contract per room. Use canonical modes from the supplied
  snapshot, not invented aliases such as continuous or co2_control. A contract
  executes only its device fields, not arbitrary extra goals in its prose label.
  BuildingOperationsApp.apply_room_plans can combine explicit plans and one
  approved 0..60-minute guarded wait. It stops on meaningful new events/alerts;
  never interpret it as permission to skip future business decisions.
  Current timestamps and meeting end_at are authoritative. A negative
  minutes_until_start means the meeting has STARTED, not that it has ended.
  At the operational horizon, resolve actual device blockers; further waiting
  cannot turn off a device. Never use printer controls on an HVAC device.
  After configuring occupied-room controls, use
  BuildingOperationsApp.monitor_environment_until_stable for a bounded,
  event-aware physical recheck; handle every returned alert or guard rejection.
After time advances or new events occur, reconcile pending room inboxes before making
further global decisions. Do not finish while the completion guard reports pending
room events or operational blockers.
""".strip()

SUPERVISOR_SYSTEM_PROMPT = (
    BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
    + "\n\n"
    + SUPERVISOR_COORDINATION_PROMPT
)

DEFAULT_SUPERVISOR_APP_NAMES = frozenset(
    {
        "AgentUserInterface",
        "BuildingOperationsApp",
        "BuildingSensorApp",
        "PrintingApp",
        "ResourceAllocationApp",
        "RoomApp",
        "ScheduleApp",
        "SystemApp",
    }
)


@dataclass(frozen=True)
class RoomEventRoute:
    event_id: str
    room_ids: tuple[str, ...]
    should_wake_agent: bool
    trigger_reason: str
    routed: bool


class RoomAgentManagerApp(App):
    """Supervisor-facing tools for discovering and controlling room agents."""

    def __init__(
        self,
        registry: RoomAgentRegistry,
        *,
        max_broadcast_rooms: int = 20,
        max_parallel_room_agents: int = 4,
    ) -> None:
        if max_broadcast_rooms <= 0:
            raise ValueError("max_broadcast_rooms must be positive")
        if max_parallel_room_agents <= 0:
            raise ValueError("max_parallel_room_agents must be positive")
        super().__init__(name="RoomAgentManagerApp")
        self.registry = registry
        self.max_broadcast_rooms = int(max_broadcast_rooms)
        self.max_parallel_room_agents = int(max_parallel_room_agents)

    @app_tool()
    @event_registered(operation_type=OperationType.READ)
    def list_room_agents(self) -> dict[str, Any]:
        """List room agents, lifecycle state, pending events, and run budgets."""

        agents = self.registry.list_agents()
        return {"status": "ok", "agents": agents, "count": len(agents)}

    @app_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_room_agent_status(self, room_id: str) -> dict[str, Any]:
        """Return one room agent's current lifecycle and inbox status."""

        try:
            return {"status": "ok", "agent": self.registry.status(room_id)}
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc), "room_id": room_id}

    @app_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_room_agent_inbox(
        self, room_id: str, limit: int = 20
    ) -> dict[str, Any]:
        """Return recent wake-worthy events waiting for one room agent."""

        try:
            events = self.registry.pending_events(room_id, limit=limit)
            return {
                "status": "ok",
                "room_id": self.registry.status(room_id)["room_id"],
                "events": events,
                "count": len(events),
            }
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc), "room_id": room_id}

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def delegate_to_room(
        self, room_id: str, task: str,
        expected_devices: list[DeviceExpectation] | None = None,
    ) -> dict[str, Any]:
        """Delegate a complete room task; supply desired device fields to verify.

        Example expected_devices: [{"device_id":"k1324_hvac_01","power_on":false}].
        Supplying expected_devices opts into automatic completion when those
        command fields match. For additional physical observation/reasoning,
        delegate a task without this contract. Device state is not comfort proof.
        """

        return self.registry.delegate(room_id, task, expected_devices)

    async def delegate_to_room_async(
        self, room_id: str, task: str,
        expected_devices: list[DeviceExpectation] | None = None,
    ) -> dict[str, Any]:
        """Delegate one task without blocking the caller's event loop."""

        return await self.registry.delegate_async(room_id, task, expected_devices)

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def broadcast_to_rooms(
        self, room_ids: list[str], task: str
    ) -> dict[str, Any]:
        """Run an explicit, bounded room set concurrently."""

        ordered_ids, rejection = self._prepare_broadcast(room_ids)
        if rejection is not None:
            return rejection
        started = time.perf_counter()
        results = self.registry.delegate_many(
            ordered_ids,
            task,
            max_concurrency=self.max_parallel_room_agents,
        )
        return self._broadcast_result(results, time.perf_counter() - started)

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def delegate_room_tasks(
        self, room_tasks: list[RoomTask]
    ) -> dict[str, Any]:
        """Run distinct independent room-local tasks with bounded concurrency.

        Decide shared power, scheduling, and priorities before dispatch; each
        room result must be verified before dependent global actions proceed.
        """

        if not room_tasks or len(room_tasks) > self.max_broadcast_rooms:
            return {
                "status": "rejected",
                "reason": "room_task_count_invalid",
                "max_broadcast_rooms": self.max_broadcast_rooms,
            }
        tasks = []
        for item in room_tasks:
            if not isinstance(item, dict):
                return {"status": "rejected", "reason": "invalid_room_task"}
            if set(item) - {"room_id", "task", "expected_devices"}:
                return {"status": "rejected", "reason": "unknown_room_task_fields"}
            room_id = item.get("room_id")
            task = item.get("task")
            if (
                not isinstance(room_id, str)
                or not isinstance(task, str)
                or not task.strip()
            ):
                return {"status": "rejected", "reason": "invalid_room_task"}
            try:
                canonical_id = self.registry.status(room_id)["room_id"]
            except ValueError:
                return {
                    "status": "rejected",
                    "reason": "unknown_room_id",
                    "room_id": room_id,
                }
            expected = item.get("expected_devices")
            rejection = self.registry.validate_expectations(canonical_id, expected)
            if rejection is not None:
                return rejection
            tasks.append({"room_id": canonical_id, "task": task, "expected_devices": canonical_goals(self.registry.world, canonical_id, expected)})
        try:
            merged = coalesce_room_tasks(tasks)
            for item in merged:
                canonical_goals(self.registry.world, item["room_id"], item["expected_devices"])
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc)}
        ordered_tasks = [(item["room_id"], item["task"], item["expected_devices"]) for item in merged]
        started = time.perf_counter()
        results = self.registry.delegate_tasks(
            ordered_tasks, max_concurrency=self.max_parallel_room_agents
        )
        result = self._broadcast_result(results, time.perf_counter() - started)
        result["coalesced_task_count"] = len(tasks) - len(merged)
        return result

    async def broadcast_to_rooms_async(
        self, room_ids: Sequence[str], task: str
    ) -> dict[str, Any]:
        """Await a bounded concurrent broadcast without blocking the event loop."""

        ordered_ids, rejection = self._prepare_broadcast(room_ids)
        if rejection is not None:
            return rejection
        started = time.perf_counter()
        results = await self.registry.delegate_many_async(
            ordered_ids,
            task,
            max_concurrency=self.max_parallel_room_agents,
        )
        return self._broadcast_result(results, time.perf_counter() - started)

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def dispatch_pending_room_events(self, task: str) -> dict[str, Any]:
        """Concurrently reconcile every room with unacknowledged events."""

        return self.dispatch_pending_rooms(self.registry.pending_room_ids(), task)

    def dispatch_pending_rooms(
        self, room_ids: Sequence[str], task: str, *, reconcile_events_only: bool = False
    ) -> dict[str, Any]:
        """Internal deterministic entry point used by automatic scheduling."""

        ordered_ids = list(dict.fromkeys(room_ids))
        if not ordered_ids:
            return {
                "status": "ok",
                "results": [],
                "room_count": 0,
                "reason": "no_pending_room_events",
            }
        if len(ordered_ids) > self.max_broadcast_rooms:
            return {
                "status": "rejected",
                "reason": "broadcast_limit_exceeded",
                "max_broadcast_rooms": self.max_broadcast_rooms,
                "requested_room_count": len(ordered_ids),
            }
        started = time.perf_counter()
        results = self.registry.delegate_many(
            ordered_ids,
            task,
            max_concurrency=self.max_parallel_room_agents,
            reconcile_events_only=reconcile_events_only,
        )
        return self._broadcast_result(results, time.perf_counter() - started)

    def _prepare_broadcast(
        self, room_ids: Sequence[str]
    ) -> tuple[list[str], dict[str, Any] | None]:
        ordered_ids = list(dict.fromkeys(room_ids))
        if not ordered_ids:
            return [], {"status": "rejected", "reason": "room_ids_cannot_be_empty"}
        if len(ordered_ids) > self.max_broadcast_rooms:
            return [], {
                "status": "rejected",
                "reason": "broadcast_limit_exceeded",
                "max_broadcast_rooms": self.max_broadcast_rooms,
                "requested_room_count": len(ordered_ids),
            }
        return ordered_ids, None

    @staticmethod
    def _broadcast_result(
        results: list[dict[str, Any]], wall_time_seconds: float | None = None
    ) -> dict[str, Any]:
        return {
            "status": (
                "ok" if all(item.get("status") == "ok" for item in results) else "partial"
            ),
            "results": results,
            "room_count": len(results),
            **(
                {"batch_wall_time_seconds": round(wall_time_seconds, 6)}
                if wall_time_seconds is not None else {}
            ),
        }

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_room_agent_enabled(
        self, room_id: str, enabled: bool
    ) -> dict[str, Any]:
        """Enable or disable future delegation to one room agent."""

        try:
            return {
                "status": "ok",
                "agent": self.registry.set_enabled(room_id, enabled),
            }
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc), "room_id": room_id}

    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def reset_room_agent(
        self, room_id: str, clear_inbox: bool = True
    ) -> dict[str, Any]:
        """Reset model memory, preserving costs/attempts/traces; optionally clear inbox."""

        try:
            return {
                "status": "ok",
                "agent": self.registry.reset(room_id, clear_inbox=clear_inbox),
            }
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc), "room_id": room_id}


class BuildingSupervisorAgent(Agent):
    """Supervisor that dispatches newly committed room events between LLM turns."""

    def __init__(
        self,
        *args: Any,
        manager: RoomAgentManagerApp,
        auto_dispatch_room_events: bool = True,
        max_auto_dispatch_rounds: int = 20,
        **kwargs: Any,
    ) -> None:
        if max_auto_dispatch_rounds <= 0:
            raise ValueError("max_auto_dispatch_rounds must be positive")
        super().__init__(*args, **kwargs)
        self.manager = manager
        self.auto_dispatch_room_events = bool(auto_dispatch_room_events)
        self.max_auto_dispatch_rounds = int(max_auto_dispatch_rounds)
        self.auto_dispatch_history: list[dict[str, Any]] = []
        self._attempted_event_ids: set[str] = set()

    def run(self, input, **kwargs):
        self.auto_dispatch_history = []
        self._attempted_event_ids = set()
        return super().run(input, **kwargs)

    def before_llm_turn(self, task: str) -> str:
        dispatch_context = self._dispatch_new_room_events()
        base_context = super().before_llm_turn(task)
        return "\n\n".join(
            item for item in (base_context, dispatch_context) if item.strip()
        )

    def _dispatch_new_room_events(self) -> str:
        if not self.auto_dispatch_room_events:
            return ""
        if len(self.auto_dispatch_history) >= self.max_auto_dispatch_rounds:
            return (
                "Automatic room-event dispatch reached its per-run round limit. "
                "Inspect RoomAgentManagerApp inbox/status tools and continue manually."
            )

        eligible_rooms: list[str] = []
        newly_attempted: set[str] = set()
        for room_id in self.manager.registry.pending_room_ids():
            events = self.manager.registry.pending_events(
                room_id,
                limit=self.manager.registry.max_inbox_events,
            )
            event_ids = {str(item.get("event_id")) for item in events}
            unseen = event_ids - self._attempted_event_ids
            if unseen:
                eligible_rooms.append(room_id)
                newly_attempted.update(unseen)
        if not eligible_rooms:
            return ""

        self._attempted_event_ids.update(newly_attempted)
        task = (
            "Reconcile every pending wake-worthy event assigned to your room. "
            "Observe current room state, perform only safe room-local actions, "
            "verify outcomes, and report blockers requiring supervisor coordination."
        )
        try:
            result = self.manager.dispatch_pending_rooms(eligible_rooms, task, reconcile_events_only=True)
        # Automatic scheduling is an isolation boundary: a scheduler defect is
        # reported to the supervisor instead of hiding the global LLM turn.
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "error",
                "reason": f"{type(exc).__name__}: {exc}",
                "results": [],
                "room_count": len(eligible_rooms),
            }
        record = {
            "round": len(self.auto_dispatch_history) + 1,
            "room_ids": eligible_rooms,
            "event_ids": sorted(newly_attempted),
            "result": result,
        }
        self.auto_dispatch_history.append(record)
        self.record_event(
            "room_event_auto_dispatch",
            status=str(result.get("status", "unknown")),
            room_ids=eligible_rooms,
            event_ids=sorted(newly_attempted),
            result=result,
        )
        return (
            "Automatic room-event reconciliation completed before this supervisor "
            f"turn:\n{json.dumps(record, ensure_ascii=False, default=str)}"
        )


class RoomEventRouter:
    """Route committed Building events into per-room inboxes."""

    def __init__(
        self,
        *,
        world: BuildingWorldApp,
        registry: RoomAgentRegistry,
        max_route_records: int = 500,
    ) -> None:
        if max_route_records <= 0:
            raise ValueError("max_route_records must be positive")
        self.world = world
        self.registry = registry
        self.max_route_records = int(max_route_records)
        self.routes: deque[RoomEventRoute] = deque()
        self._attached_runtime_ids: set[int] = set()

    def attach(self, runtime: BuildingWorldRuntime) -> None:
        """Subscribe once to post-classification Runtime events."""

        runtime_id = id(runtime)
        if runtime_id in self._attached_runtime_ids:
            return
        runtime.register_trigger_listener(self.route)
        self._attached_runtime_ids.add(runtime_id)

    def route(
        self, event: BuildingEvent, decision: TriggerDecision
    ) -> RoomEventRoute:
        resolved_room_ids = resolve_event_room_ids(self.world, event)
        registered = set(self.registry.registered_room_ids())
        room_ids = tuple(
            room_id for room_id in resolved_room_ids if room_id in registered
        )
        routed_room_ids = []
        if decision.should_wake_agent:
            for room_id in room_ids:
                if (
                    event.event_type == BuildingEventType.COMFORT_THRESHOLD_VIOLATED
                    and self.world.room_states[room_id].occupancy_count == 0
                ):
                    continue
                self.registry.enqueue_event(
                    room_id,
                    event,
                    should_wake_agent=True,
                    trigger_reason=decision.reason,
                )
                routed_room_ids.append(room_id)
        result = RoomEventRoute(
            event_id=event.event_id,
            room_ids=room_ids,
            should_wake_agent=decision.should_wake_agent,
            trigger_reason=decision.reason,
            routed=bool(routed_room_ids),
        )
        if len(self.routes) >= self.max_route_records:
            self.routes.popleft()
        self.routes.append(result)
        return result

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "routes": [
                {
                    "event_id": route.event_id,
                    "room_ids": list(route.room_ids),
                    "should_wake_agent": route.should_wake_agent,
                    "trigger_reason": route.trigger_reason,
                    "routed": route.routed,
                }
                for route in self.routes
            ],
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        if int(snapshot.get("schema_version", 0)) != 1:
            raise ValueError("unsupported room-event-router snapshot schema_version")
        raw_routes = snapshot.get("routes", [])
        if not isinstance(raw_routes, list):
            raise TypeError("room-event-router snapshot routes must be a list")
        self.routes.clear()
        for raw in raw_routes[-self.max_route_records :]:
            if not isinstance(raw, Mapping):
                raise TypeError("invalid room-event route snapshot")
            self.routes.append(
                RoomEventRoute(
                    event_id=str(raw["event_id"]),
                    room_ids=tuple(str(item) for item in raw.get("room_ids", [])),
                    should_wake_agent=bool(raw.get("should_wake_agent", False)),
                    trigger_reason=str(raw.get("trigger_reason", "")),
                    routed=bool(raw.get("routed", False)),
                )
            )


class BuildingMultiAgentSystem:
    """Opt-in supervisor plus room-agent control layer."""

    def __init__(
        self,
        *,
        world: BuildingWorldApp,
        runtime: BuildingWorldRuntime,
        registry: RoomAgentRegistry,
        manager: RoomAgentManagerApp,
        router: RoomEventRouter,
        supervisor: Agent,
        max_supervisor_tool_calls: int = 40,
        supervisor_timeout_seconds: float | None = None,
        max_supervisor_traces: int = 20,
    ) -> None:
        if max_supervisor_tool_calls <= 0 or max_supervisor_traces <= 0:
            raise ValueError("supervisor limits must be positive")
        self.world = world
        self.runtime = runtime
        self.registry = registry
        self.manager = manager
        self.router = router
        self.supervisor = supervisor
        self.max_supervisor_tool_calls = int(max_supervisor_tool_calls)
        self.supervisor_timeout_seconds = supervisor_timeout_seconds
        self.max_supervisor_traces = int(max_supervisor_traces)
        self.supervisor_traces: deque[dict[str, Any]] = deque()
        self._supervisor_run_lock = Lock()
        self._engine: Any | None = None
        self._external_completion_guard: Callable[[], Mapping[str, Any] | None] | None = None

    def register_time_manager(self, time_manager: Any) -> None:
        self.registry.register_time_manager(time_manager)
        self.manager.register_time_manager(time_manager)
        self.supervisor.set_time_manager(time_manager)

    def attach_engine(self, engine: Any) -> None:
        """Bind the shared clock and compose Engine's operational completion guard."""

        if getattr(engine, "agent", None) is not self.supervisor:
            raise ValueError("engine.agent must be this multi-agent supervisor")
        self._engine = engine
        self._synchronize_clock(engine.time_manager, engine=engine)
        self.register_time_manager(engine.time_manager)
        current_guard = getattr(self.supervisor, "completion_guard", None)
        is_own_guard = (
            getattr(current_guard, "__self__", None) is self
            and getattr(current_guard, "__func__", None)
            is BuildingMultiAgentSystem.completion_status
        )
        if not is_own_guard:
            self._external_completion_guard = current_guard
        self.supervisor.completion_guard = self.completion_status
        operations = next((app for app in self.registry.building_apps if isinstance(app, BuildingOperationsApp)), None)
        if operations is not None:
            operations.routine_event_acknowledger = self._acknowledge_routine_wait_events

    def _acknowledge_routine_wait_events(self, event_ids: set[str]) -> bool:
        """Acknowledge only facts already verified by the common wait policy."""
        for session in self.registry._sessions.values():
            with session.state_lock:
                session.inbox = deque(e for e in session.inbox if str(e.get("event_id")) not in event_ids)
        return not self.registry.pending_room_ids()

    def completion_status(self) -> dict[str, Any]:
        """Combine scenario blockers with unacknowledged room events."""

        base = (
            self._external_completion_guard()
            if self._external_completion_guard is not None
            else None
        )
        result = dict(base) if isinstance(base, Mapping) else {"status": "ok"}
        blockers = [dict(item) for item in result.get("blockers", [])]
        pending_rooms = []
        for room_id in self.registry.pending_room_ids():
            events = self.registry.pending_events(
                room_id,
                limit=self.registry.max_inbox_events,
            )
            pending_rooms.append(
                {
                    "room_id": room_id,
                    "event_ids": [str(item.get("event_id")) for item in events],
                }
            )
        if pending_rooms:
            blockers.append(
                {
                    "code": "room_agent_events_pending",
                    "rooms": pending_rooms,
                }
            )
        result["blockers"] = blockers
        result["can_finish"] = bool(result.get("can_finish", not blockers)) and not blockers
        result["pending_room_agent_count"] = len(pending_rooms)
        return result

    def room_runtime_metrics(self) -> list[dict[str, Any]]:
        return self.registry.runtime_metrics()

    def room_metric_totals(self) -> dict[str, Any]:
        return self.registry.cumulative_metric_totals()

    def telemetry(self) -> dict[str, Any]:
        agents = self.registry.list_agents()
        return {
            "mode": "multi",
            "room_agent_count": len(agents),
            "initialized_room_agent_count": sum(
                1 for item in agents if item["initialized"]
            ),
            "room_run_count": sum(int(item["run_count"]) for item in agents),
            "room_reset_count": sum(int(item["reset_count"]) for item in agents),
            "room_resolution_counts": dict(Counter(
                entry["status"] for ledger in self.registry.all_run_ledgers().values() for entry in ledger
            )),
            "room_failure_count": sum(
                int(item["failure_count"]) for item in agents
            ),
            "room_tool_call_count": sum(
                int(item["tool_call_count_total"]) for item in agents
            ),
            "room_llm_turn_count": sum(
                int(item["llm_turn_count_total"]) for item in agents
            ),
            "pending_room_ids": list(self.registry.pending_room_ids()),
            "auto_dispatch_round_count": len(
                getattr(self.supervisor, "auto_dispatch_history", [])
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        """Serialize Runtime and control state as one JSON-safe checkpoint."""

        return {
            "schema_version": 1,
            "runtime": _checkpoint_encode(self.runtime.snapshot()),
            "registry": self.registry.snapshot(),
            "router": self.router.snapshot(),
            "supervisor_traces": _json_safe(list(self.supervisor_traces)),
        }

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        if int(snapshot.get("schema_version", 0)) != 1:
            raise ValueError("unsupported multi-agent snapshot schema_version")
        runtime_snapshot = snapshot.get("runtime")
        registry_snapshot = snapshot.get("registry")
        router_snapshot = snapshot.get("router")
        if not isinstance(registry_snapshot, Mapping) or not isinstance(
            router_snapshot, Mapping
        ):
            raise TypeError("multi-agent snapshot requires registry and router")
        if runtime_snapshot is not None:
            if not isinstance(runtime_snapshot, Mapping):
                raise TypeError("multi-agent runtime snapshot must be a mapping")
            decoded_runtime = _checkpoint_decode(runtime_snapshot)
            if not isinstance(decoded_runtime, Mapping):
                raise TypeError("decoded runtime snapshot must be a mapping")
            self.runtime.restore(decoded_runtime)
            time_manager = (
                self._engine.time_manager
                if self._engine is not None
                else self.registry.time_manager
            )
            self._synchronize_clock(time_manager, engine=self._engine)
            self.register_time_manager(time_manager)
        self.registry.restore(registry_snapshot)
        self.router.restore(router_snapshot)
        raw_traces = snapshot.get("supervisor_traces", [])
        if not isinstance(raw_traces, list):
            raise TypeError("supervisor_traces must be a list")
        self.supervisor_traces = deque(
            dict(item) for item in raw_traces[-self.max_supervisor_traces :]
        )

    def _synchronize_clock(self, time_manager: Any, *, engine: Any | None) -> None:
        reset = getattr(time_manager, "reset", None)
        if not callable(reset):
            raise TypeError("multi-agent TimeManager must provide reset()")
        reset(start_time=self.runtime.current_time.timestamp())
        if engine is not None:
            engine.current_time = time_manager.time()

    def run_supervisor(self, task: str) -> dict[str, Any]:
        """Run one bounded supervisor request with a fresh model context."""

        if not str(task).strip():
            return {"status": "rejected", "reason": "task_cannot_be_empty"}
        with self._supervisor_run_lock:
            return self._run_supervisor_session(str(task))

    async def run_supervisor_async(self, task: str) -> dict[str, Any]:
        """Run the synchronous supervisor loop without blocking an event loop."""

        return await asyncio.to_thread(self.run_supervisor, task)

    def _run_supervisor_session(self, task: str) -> dict[str, Any]:
        self.supervisor.messages = Messages(
            provider=getattr(self.supervisor.llm, "provider", "openai"),
            system_message=self.supervisor.system_message,
        )
        self.supervisor.workflow = Workflow()
        self.supervisor.runtime_events = []
        self.supervisor.tool_call_count = 0
        self.supervisor.llm_turn_count = 0
        try:
            answer = self.supervisor.run(
                task,
                max_tool_calls=self.max_supervisor_tool_calls,
                timeout_seconds=self.supervisor_timeout_seconds,
            )
            result = self._supervisor_result("ok", answer=str(answer or ""))
        except AgentRunLimitExceeded as exc:
            result = self._supervisor_result(
                "stopped",
                answer=None,
                reason=f"{exc.limit_type}={exc.limit_value}",
            )
        # LLM/provider failures are returned as one isolated supervisor result.
        except Exception as exc:  # noqa: BLE001
            result = self._supervisor_result(
                "error",
                answer=None,
                reason=f"{type(exc).__name__}: {exc}",
            )
        if len(self.supervisor_traces) >= self.max_supervisor_traces:
            self.supervisor_traces.popleft()
        self.supervisor_traces.append(
            {
                **result,
                "runtime_events": list(self.supervisor.runtime_events),
                "messages": list(self.supervisor.messages.messages),
                "workflow": self.supervisor.workflow.to_dict(),
            }
        )
        return result

    def _supervisor_result(
        self,
        status: str,
        *,
        answer: str | None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "answer": answer,
            "reason": reason,
            "tool_call_count": self.supervisor.tool_call_count,
            "llm_turn_count": self.supervisor.llm_turn_count,
        }


def build_building_multi_agent_system(
    *,
    world: BuildingWorldApp,
    runtime: BuildingWorldRuntime,
    supervisor_llm: Any,
    room_llm_factory: Callable[[str], Any],
    building_apps: Sequence[Any] = (),
    supervisor_toolsets: Sequence[Any] = (),
    allowed_room_tool_names: set[str] | frozenset[str] | None = None,
    time_manager: Any | None = None,
    default_room_max_tool_calls: int = 20,
    room_timeout_seconds: float | None = None,
    max_prompt_chars: int | None = None,
    max_repeated_identical_calls: int | None = None,
    max_supervisor_tool_calls: int = 40,
    supervisor_timeout_seconds: float | None = None,
    max_inbox_events: int = 50,
    max_broadcast_rooms: int = 20,
    max_parallel_room_agents: int = 4,
    auto_dispatch_room_events: bool = True,
    max_auto_dispatch_rounds: int = 20,
    supervisor_system_message: str = SUPERVISOR_SYSTEM_PROMPT,
) -> BuildingMultiAgentSystem:
    """Assemble an opt-in supervisor and one lazy logical agent per room."""

    effective_time_manager = time_manager or world.time_manager
    registry = RoomAgentRegistry(
        world=world,
        llm_factory=room_llm_factory,
        building_apps=building_apps,
        allowed_tool_names=allowed_room_tool_names,
        time_manager=effective_time_manager,
        default_max_tool_calls=default_room_max_tool_calls,
        default_timeout_seconds=room_timeout_seconds,
        max_prompt_chars=max_prompt_chars,
        max_repeated_identical_calls=max_repeated_identical_calls,
        max_inbox_events=max_inbox_events,
    )
    registry.register_all_rooms()
    registry.register_time_manager(effective_time_manager)
    manager = RoomAgentManagerApp(
        registry,
        max_broadcast_rooms=max_broadcast_rooms,
        max_parallel_room_agents=max_parallel_room_agents,
    )
    manager.register_time_manager(effective_time_manager)
    router = RoomEventRouter(world=world, registry=registry)
    router.attach(runtime)
    supervisor = BuildingSupervisorAgent(
        name="building_supervisor_agent",
        llm=supervisor_llm,
        system_message=supervisor_system_message,
        toolsets=[world, manager, *supervisor_toolsets],
        manager=manager,
        auto_dispatch_room_events=auto_dispatch_room_events,
        max_auto_dispatch_rounds=max_auto_dispatch_rounds,
    )
    supervisor.max_prompt_chars = max_prompt_chars
    supervisor.max_repeated_identical_calls = max_repeated_identical_calls
    supervisor.set_time_manager(effective_time_manager)
    return BuildingMultiAgentSystem(
        world=world,
        runtime=runtime,
        registry=registry,
        manager=manager,
        router=router,
        supervisor=supervisor,
        max_supervisor_tool_calls=max_supervisor_tool_calls,
        supervisor_timeout_seconds=supervisor_timeout_seconds,
    )


def select_supervisor_toolsets(
    apps: Sequence[Any],
    *,
    allowed_app_names: set[str] | frozenset[str] | None = None,
) -> tuple[Any, ...]:
    """Select global/cross-room Apps without exposing local device controls."""

    allowed = (
        DEFAULT_SUPERVISOR_APP_NAMES
        if allowed_app_names is None
        else frozenset(allowed_app_names)
    )
    selected: list[Any] = []
    seen: set[int] = set()
    for app in apps:
        names = {
            str(getattr(app, "name", "")),
            app.__class__.__name__,
        }
        if names.isdisjoint(allowed) or id(app) in seen:
            continue
        selected.append(app)
        seen.add(id(app))
    return tuple(selected)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _checkpoint_encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {
            "__fairy_checkpoint_type__": "datetime",
            "value": value.isoformat(),
        }
    if isinstance(value, SensorReading):
        return {
            "__fairy_checkpoint_type__": "sensor_reading",
            "sensor_id": value.sensor_id,
            "zone_id": value.zone_id,
            "quantity": value.quantity.value,
            "value": value.value,
            "unit": value.unit,
            "observed_at": _checkpoint_encode(value.observed_at),
            "available_at": _checkpoint_encode(value.available_at),
            "quality": value.quality.value,
            "metadata": _checkpoint_encode(dict(value.metadata)),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _checkpoint_encode(item) for key, item in value.items()
        }
    if isinstance(value, tuple):
        return {
            "__fairy_checkpoint_type__": "tuple",
            "items": [_checkpoint_encode(item) for item in value],
        }
    if isinstance(value, list):
        return [_checkpoint_encode(item) for item in value]
    return value


def _checkpoint_decode(value: Any) -> Any:
    if isinstance(value, Mapping):
        checkpoint_type = value.get("__fairy_checkpoint_type__")
        if (
            checkpoint_type == "datetime"
            and set(value) == {"__fairy_checkpoint_type__", "value"}
        ):
            return datetime.fromisoformat(str(value["value"]))
        if checkpoint_type == "tuple" and set(value) == {
            "__fairy_checkpoint_type__",
            "items",
        }:
            return tuple(_checkpoint_decode(item) for item in value["items"])
        if checkpoint_type == "sensor_reading":
            observed_at = _checkpoint_decode(value["observed_at"])
            available_at = _checkpoint_decode(value.get("available_at"))
            if not isinstance(observed_at, datetime) or not (
                available_at is None or isinstance(available_at, datetime)
            ):
                raise ValueError("invalid SensorReading checkpoint timestamps")
            metadata = _checkpoint_decode(value.get("metadata", {}))
            if not isinstance(metadata, Mapping):
                raise ValueError("invalid SensorReading checkpoint metadata")
            return SensorReading(
                sensor_id=str(value["sensor_id"]),
                zone_id=str(value["zone_id"]),
                quantity=SensorQuantity(str(value["quantity"])),
                value=float(value["value"]),
                unit=str(value["unit"]),
                observed_at=observed_at,
                available_at=available_at,
                quality=SensorQuality(str(value["quality"])),
                metadata={str(key): str(item) for key, item in metadata.items()},
            )
        return {str(key): _checkpoint_decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_checkpoint_decode(item) for item in value]
    return value
