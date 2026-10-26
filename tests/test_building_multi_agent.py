import asyncio
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from fairy.agents.agent.toolset_builder import build_toolset
from fairy.apps.building_world import (
    BuildingOperationsApp,
    DeviceRegistryApp,
    HvacApp,
    VentilationApp,
)
from fairy.apps.building_world.room_loader import load_room_configuration
from fairy.apps.building_world.runtime import (
    BuildingWorldRuntime,
    constant_outdoor_provider,
)
from fairy.apps.building_world.types import BuildingEventType, MeetingStatus
from fairy.controllers.building import (
    DEFAULT_ROOM_TOOL_NAMES,
    RoomAgentManagerApp,
    RoomAgentRegistry,
    RoomContextApp,
    RoomEventRouter,
    RoomScopedToolset,
    RoomScopeViolation,
    build_building_multi_agent_system,
)
from fairy.controllers.building.trigger_policy import TriggerDecision
from fairy.controllers.engine import Engine
from fairy.physics.building import OutdoorConditions
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES
from fairy.tool_utils import _json_schema

CONFIG_DIR = Path(__file__).parents[1] / "fairy" / "configs" / "rooms"
START_AT = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)


def test_batch_advertises_object_items_and_device_postconditions() -> None:
    registry = RoomAgentRegistry(world=_runtime().world, llm_factory=lambda _: None)
    registry.register_all_rooms()
    _, schemas, _ = build_toolset([RoomAgentManagerApp(registry)])
    schema = next(
        item["function"]["parameters"]
        for item in schemas
        if item["function"]["name"].endswith("__delegate_room_tasks")
    )
    task = schema["properties"]["room_tasks"]["items"]
    assert task["type"] == "object"
    assert set(task["required"]) == {"room_id", "task"}
    assert task["additionalProperties"] is False
    device = task["properties"]["expected_devices"]["items"]
    assert device["type"] == "object"
    assert device["required"] == ["device_id"]
    assert device["properties"]["power_on"]["type"] == "boolean"
    assert _json_schema(list[dict[str, str]]) == {
        "type": "array",
        "items": {"type": "object", "additionalProperties": {"type": "string"}},
    }


@pytest.mark.parametrize("perform_shutdown", [False, True])
def test_closeout_checks_actual_state_and_retains_unresolved_events(
    perform_shutdown,
) -> None:
    scenario = L3_SCENARIO_CLASSES[18]()
    scenario.setup()
    engine = Engine(None, scenario, virtual_time=True)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    runtime = scenario.building_runtime
    world = runtime.world
    runtime.current_time = operations.operational_end_at
    engine.time_manager.set_offset(
        operations.operational_end_at.timestamp() - scenario.start_time
    )
    for state in world.room_states.values():
        state.occupancy_count = 0
    for state in world.device_states.values():
        state.power_on = False
    world.device_states["k1324_hvac_01"].power_on = True
    scripted = [_Message(content="All devices are already off.")]
    if perform_shutdown:
        scripted.extend(
            [
                _Message(
                    tool_calls=[
                        _ToolCall(
                            "HvacApp__set_hvac",
                            json.dumps(
                                {
                                    "device_id": "k1324_hvac_01",
                                    "power_on": False,
                                    "mode": "off",
                                    "target_temperature_c": 24.0,
                                    "fan_level": 0,
                                }
                            ),
                        )
                    ]
                ),
                _Message(content="Shutdown verified."),
            ]
        )
    registry = RoomAgentRegistry(
        world=world,
        llm_factory=lambda _: _ScriptedLLM(scripted),
        building_apps=scenario.apps,
        time_manager=engine.time_manager,
    )
    registry.register_all_rooms()
    event = world.publish_building_event(
        BuildingEventType.MANUAL_OVERRIDE, source="test", subject_id="k1324_hvac_01"
    )
    registry.enqueue_event(
        "k1324", event, should_wake_agent=True, trigger_reason="closeout"
    )

    # No explicit contract: the public operational-horizon invariant still
    # catches the exact v2 scene-19 failure, including zero-tool claims.
    result = registry.delegate("k1324", "Shut down and verify the room.")

    assert result["verification"]["can_finish"] is perform_shutdown
    assert result["llm_turn_count"] == 3
    assert result["status"] == ("ok" if perform_shutdown else "stopped")
    assert result["pending_event_count"] == (0 if perform_shutdown else 1)
    assert result["processed_event_ids"] == (
        [event.event_id] if perform_shutdown else []
    )
    assert result["tool_call_count"] == (1 if perform_shutdown else 0)
    if not perform_shutdown:
        assert result["reason"] == "completion_verification=3"
        assert "k1324_hvac_01" in str(result["verification"]["blockers"])
        terminal = registry.traces("k1324")[0]["trace"]["runtime_events"][-1]
        assert terminal["event_type"] == "agent_run_end"
        assert terminal["status"] == "stopped"


def test_explicit_device_contract_rejects_false_claim_before_closeout() -> None:
    runtime = _runtime()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda _: _ScriptedLLM([_Message(content="Heating enabled.")]),
    )
    registry.register_all_rooms()
    result = registry.delegate(
        "k1324",
        "Enable heating.",
        [
            {
                "device_id": "k1324_hvac_01",
                "power_on": True,
                "mode": "heating",
            }
        ],
    )
    assert result["status"] == "stopped"
    assert result["verification"]["can_finish"] is False
    assert result["tool_call_count"] == 0


def test_batch_contracts_are_atomic_and_skip_already_satisfied_goals() -> None:
    runtime = _runtime()
    probe = _ConcurrencyProbe()
    registry = RoomAgentRegistry(
        world=runtime.world, llm_factory=lambda room_id: _DelayedLLM(room_id, probe)
    )
    registry.register_all_rooms()
    manager = RoomAgentManagerApp(registry, max_parallel_room_agents=2)
    tasks = [
        {
            "room_id": room_id,
            "task": "Verify powered off.",
            "expected_devices": [
                {"device_id": f"{room_id}_hvac_01", "power_on": False}
            ],
        }
        for room_id in ("k1324", "k1315")
    ]
    tasks[1]["expected_devices"][0]["device_id"] = "k1324_hvac_01"
    assert manager.delegate_room_tasks(tasks)["reason"] == "invalid_device_expectations"
    assert all(item["run_count"] == 0 for item in registry.list_agents())
    tasks[1]["expected_devices"][0]["device_id"] = "k1315_hvac_01"
    result = manager.delegate_room_tasks(tasks)
    assert result["status"] == "ok"
    assert probe.max_active == 0
    assert all(item["llm_turn_count"] == 0 for item in result["results"])
    assert all(item["verification"]["can_finish"] for item in result["results"])


def test_fresh_context_replaces_unverified_previous_prose_and_compacts_handoff() -> (
    None
):
    runtime = _runtime()
    answer = "x" * 2000
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda _: _ScriptedLLM([_Message(content=answer)]),
    )
    registry.register_all_rooms()
    registry._session("k1324").last_answer = "STALE_UNVERIFIED_CLAIM"
    result = registry.delegate("k1324", "Inspect current room state.")
    trace = registry.traces("k1324")[0]
    prompt = trace["trace"]["messages"][1]["content"]
    assert "STALE_UNVERIFIED_CLAIM" not in prompt
    assert "Fresh canonical room state" in prompt
    assert "k1324_hvac_01" in prompt
    assert len(result["answer"]) == 600
    assert trace["answer"] == answer  # Full audit data is not truncated.
    assert result["verification"]["scope"].startswith("state_observed_only")


def test_room_agents_cannot_cancel_shared_meetings_as_cleanup() -> None:
    assert "ScheduleApp__cancel_meeting" not in DEFAULT_ROOM_TOOL_NAMES


class _PromptDetails:
    cached_tokens = 0


class _Usage:
    prompt_tokens = 5
    completion_tokens = 2
    total_tokens = 7
    prompt_tokens_details = _PromptDetails()


class _FunctionCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    type = "function"

    def __init__(self, name: str, arguments: str = "{}") -> None:
        self.id = f"call_{name}"
        self.function = _FunctionCall(name, arguments)


class _Message:
    def __init__(self, content: str | None = None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    usage = _Usage()

    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


class _ScriptedLLM:
    provider = "openai"
    model = "scripted-building-multi-agent"

    def __init__(self, messages: list[_Message]) -> None:
        self.messages = list(messages)

    def chat_completion(self, messages, tools=None):
        if not self.messages:
            return _Response(_Message(content="done", tool_calls=None))
        return _Response(self.messages.pop(0))


class _ConcurrencyProbe:
    def __init__(self, delay_seconds: float = 0.08) -> None:
        self.delay_seconds = delay_seconds
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


class _DelayedLLM:
    provider = "openai"
    model = "delayed-building-multi-agent"

    def __init__(self, room_id: str, probe: _ConcurrencyProbe) -> None:
        self.room_id = room_id
        self.probe = probe

    def chat_completion(self, messages, tools=None):
        self.probe.enter()
        try:
            time.sleep(self.probe.delay_seconds)
            return _Response(
                _Message(content=f"{self.room_id} complete", tool_calls=None)
            )
        finally:
            self.probe.exit()


class _FailingLLM:
    provider = "openai"
    model = "failing-building-multi-agent"

    def __init__(self) -> None:
        self.call_count = 0

    def chat_completion(self, messages, tools=None):
        self.call_count += 1
        raise RuntimeError("room provider unavailable")


def _runtime() -> BuildingWorldRuntime:
    configurations = tuple(
        load_room_configuration(CONFIG_DIR / filename)
        for filename in ("k1324.yaml", "k1316.yaml", "k1315.yaml")
    )
    return BuildingWorldRuntime.from_room_configurations(
        configurations,
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


def test_room_context_exposes_only_assigned_room() -> None:
    runtime = _runtime()
    world = runtime.world
    world.publish_building_event(
        BuildingEventType.MANUAL_OVERRIDE,
        source="operator",
        subject_id="k1324_hvac_01",
    )
    world.publish_building_event(
        BuildingEventType.MANUAL_OVERRIDE,
        source="operator",
        subject_id="k1315_hvac_01",
    )

    context = RoomContextApp(world, "K1324")
    snapshot = context.get_room_snapshot()
    events = context.get_recent_room_events()

    assert snapshot["room"]["room_id"] == "k1324"
    assert {item["zone_id"] for item in snapshot["zones"]} == {
        "k1324_office_zone"
    }
    assert snapshot["devices"]
    assert all(item["device_id"].startswith("k1324_") for item in snapshot["devices"])
    assert [item["subject_id"] for item in events["events"]] == [
        "k1324_hvac_01"
    ]


def test_room_scoped_toolset_allows_local_and_rejects_cross_room_resources() -> None:
    runtime = _runtime()
    scoped = RoomScopedToolset(
        world=runtime.world,
        room_id="k1324",
        apps=[HvacApp(runtime.world), DeviceRegistryApp(runtime.world)],
    )
    tools = {
        tool.to_open_ai()["function"]["name"]: tool for tool in scoped.get_tools()
    }

    accepted = tools["HvacApp__set_hvac"](
        device_id="k1324_hvac_01",
        power_on=True,
        mode="cooling",
        target_temperature_c=24.0,
        fan_level=2,
    )
    assert accepted["status"] == "accepted"

    with pytest.raises(RoomScopeViolation, match="belongs to room 'k1315'"):
        tools["HvacApp__set_hvac"](
            device_id="k1315_hvac_01",
            power_on=True,
            mode="cooling",
            target_temperature_c=24.0,
            fan_level=2,
        )

    with pytest.raises(RoomScopeViolation, match="no room-scoped resource"):
        tools["DeviceRegistryApp__list_devices"]()


def test_delegated_power_increase_is_rejected_before_mutating_device() -> None:
    scenario = L3_SCENARIO_CLASSES[15]()
    scenario.setup()
    Engine(None, scenario)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    hvac = scenario.get_typed_app(HvacApp)
    for room_id in ("k1315", "k1316"):
        hvac.set_hvac(f"{room_id}_hvac_01", True, "cooling", 24.0, 3)

    scoped = RoomScopedToolset(
        world=operations.world,
        room_id="k1324",
        apps=[hvac, operations],
    )
    tool = next(
        item
        for item in scoped.get_tools()
        if item.to_open_ai()["function"]["name"] == "HvacApp__set_hvac"
    )
    result = tool(
        device_id="k1324_hvac_01",
        power_on=True,
        mode="cooling",
        target_temperature_c=24.0,
        fan_level=3,
    )
    assert result["reason"] == "power_budget_exceeded"
    assert not operations.world.device_states["k1324_hvac_01"].power_on


def test_delegated_room_cannot_disable_ventilation_before_meeting() -> None:
    scenario = L3_SCENARIO_CLASSES[19]()
    scenario.setup()
    Engine(None, scenario)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    ventilation = scenario.get_typed_app(VentilationApp)
    meeting = operations.world.schedule["priority-seminar-one"]
    operations.runtime.current_time = meeting.start_at - timedelta(minutes=4)
    ventilation.set_ventilation("k1316_ventilation_01", True, 1)
    scoped = RoomScopedToolset(
        world=operations.world,
        room_id="k1316",
        apps=[ventilation, operations],
    )
    tool = next(
        item
        for item in scoped.get_tools()
        if item.to_open_ai()["function"]["name"]
        == "VentilationApp__set_ventilation"
    )
    result = tool(device_id="k1316_ventilation_01", power_on=False, level=0)
    assert result["reason"] == "meeting_ventilation_required"
    assert operations.world.device_states["k1316_ventilation_01"].power_on

    meeting.status = MeetingStatus.COMPLETED
    operations.world.room_states["k1316"].occupancy_count = 8
    result = tool(device_id="k1316_ventilation_01", power_on=False, level=0)
    assert result["reason"] == "meeting_ventilation_required"
    assert result["occupancy_count"] == 8
    assert operations.world.device_states["k1316_ventilation_01"].power_on


def test_delegated_room_cannot_start_early_idle_equipment() -> None:
    scenario = L3_SCENARIO_CLASSES[16]()
    scenario.setup()
    Engine(None, scenario)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    hvac = scenario.get_typed_app(HvacApp)
    scoped = RoomScopedToolset(
        world=operations.world,
        room_id="k1315",
        apps=[hvac, operations],
    )
    tool = next(
        item
        for item in scoped.get_tools()
        if item.to_open_ai()["function"]["name"] == "HvacApp__set_hvac"
    )
    result = tool(
        device_id="k1315_hvac_01",
        power_on=True,
        mode="cooling",
        target_temperature_c=22.0,
        fan_level=2,
    )
    assert result["reason"] == "unoccupied_energy_risk"
    assert not operations.world.device_states["k1315_hvac_01"].power_on


def test_room_registry_is_lazy_isolated_and_acknowledges_events_on_success() -> None:
    runtime = _runtime()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _ScriptedLLM(
            [_Message(content=f"{room_id} complete", tool_calls=None)]
        ),
    )
    registry.register_all_rooms()
    event = runtime.world.publish_building_event(
        BuildingEventType.DEVICE_FAILED,
        source="monitor",
        subject_id="k1324_hvac_01",
        payload={"fault": "compressor_trip"},
    )
    registry.enqueue_event(
        "k1324",
        event,
        should_wake_agent=True,
        trigger_reason="plan_or_environment_may_require_adaptation",
    )

    result = registry.delegate("k1324", "Inspect the local HVAC failure.")

    assert result["status"] == "ok"
    assert result["answer"] == "k1324 complete"
    assert result["processed_event_ids"] == [event.event_id]
    assert registry.status("k1324")["pending_event_count"] == 0
    assert registry.status("k1324")["initialized"] is True
    assert registry.status("k1315")["initialized"] is False
    user_message = registry.traces("k1324")[0]["trace"]["messages"][1]["content"]
    assert "k1324_hvac_01" in user_message
    assert "Assigned room: k1324" in user_message


def test_room_registry_retains_inbox_when_budget_stops_agent() -> None:
    runtime = _runtime()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda _room_id: _ScriptedLLM(
            [
                _Message(
                    tool_calls=[_ToolCall("RoomContextApp__get_room_snapshot")]
                ),
                _Message(content="would finish", tool_calls=None),
            ]
        ),
        default_max_tool_calls=1,
    )
    registry.register_all_rooms()
    event = runtime.world.publish_building_event(
        BuildingEventType.MANUAL_OVERRIDE,
        source="operator",
        subject_id="k1324_hvac_01",
    )
    registry.enqueue_event(
        "k1324",
        event,
        should_wake_agent=True,
        trigger_reason="explicit_event_override",
    )

    result = registry.delegate("k1324", "Inspect and report.")

    assert result["status"] == "stopped", result
    assert result["reason"] == "max_tool_calls=1"
    assert registry.status("k1324")["pending_event_count"] == 1
    assert registry.status("k1324")["failure_count"] == 1


def test_runtime_routes_event_to_owning_room_and_isolates_listener_failure() -> None:
    runtime = _runtime()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda _room_id: _ScriptedLLM([_Message(content="done")]),
    )
    registry.register_all_rooms()
    router = RoomEventRouter(world=runtime.world, registry=registry)
    router.attach(runtime)

    def failing_listener(event, decision) -> None:
        raise RuntimeError("observer failed")

    runtime.register_trigger_listener(failing_listener)
    execute_at = START_AT + timedelta(minutes=1)
    runtime.schedule_event(
        execute_at=execute_at,
        event_type=BuildingEventType.DEVICE_FAILED,
        source="monitor",
        subject_id="k1315_hvac_01",
    )

    result = runtime.advance_to(START_AT + timedelta(minutes=2))

    failure_event = next(
        event
        for event in result.processed_events
        if event.event_type == BuildingEventType.DEVICE_FAILED
    )
    pending_k1315 = registry.pending_events("k1315", limit=10)
    assert failure_event.event_id in {item["event_id"] for item in pending_k1315}
    pending_k1324 = registry.pending_events("k1324", limit=10)
    assert failure_event.event_id not in {item["event_id"] for item in pending_k1324}
    assert any(
        route.event_id == failure_event.event_id
        and route.room_ids == ("k1315",)
        and route.should_wake_agent
        for route in router.routes
    )
    assert any(item.get("kind") == "trigger_listener_error" for item in runtime.trace)


def test_unoccupied_comfort_alert_does_not_auto_dispatch_room_agent() -> None:
    runtime = _runtime()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda _room_id: _ScriptedLLM([_Message(content="done")]),
    )
    registry.register_all_rooms()
    router = RoomEventRouter(world=runtime.world, registry=registry)
    event = runtime.world.publish_building_event(
        BuildingEventType.COMFORT_THRESHOLD_VIOLATED,
        source="monitor",
        subject_id="k1316_seminar_zone",
    )
    decision = TriggerDecision(
        event.event_id, event.event_type, True, "comfort_threshold"
    )
    route = router.route(event, decision)
    assert route.room_ids == ("k1316",)
    assert route.routed is False
    assert registry.pending_events("k1316") == []

    runtime.world.room_states["k1316"].occupancy_count = 1
    occupied_event = runtime.world.publish_building_event(
        BuildingEventType.COMFORT_THRESHOLD_VIOLATED,
        source="monitor",
        subject_id="k1316_seminar_zone",
    )
    occupied_decision = TriggerDecision(
        occupied_event.event_id, occupied_event.event_type, True, "comfort_threshold"
    )
    assert router.route(occupied_event, occupied_decision).routed is True
    assert len(registry.pending_events("k1316")) == 1


def test_supervisor_delegates_to_room_agent_through_manager_tool() -> None:
    runtime = _runtime()
    supervisor_llm = _ScriptedLLM(
        [
            _Message(
                tool_calls=[
                    _ToolCall(
                        "RoomAgentManagerApp__delegate_to_room",
                        json.dumps(
                            {
                                "room_id": "k1316",
                                "task": "Check seminar room readiness.",
                            }
                        ),
                    )
                ]
            ),
            _Message(content="Seminar room delegation completed.", tool_calls=None),
        ]
    )
    system = build_building_multi_agent_system(
        world=runtime.world,
        runtime=runtime,
        supervisor_llm=supervisor_llm,
        room_llm_factory=lambda room_id: _ScriptedLLM(
            [_Message(content=f"{room_id} ready", tool_calls=None)]
        ),
    )

    result = system.run_supervisor("Prepare K1316 through its room agent.")

    assert result["status"] == "ok", result
    assert result["answer"] == "Seminar room delegation completed."
    assert result["tool_call_count"] == 1
    assert system.registry.status("k1316")["run_count"] == 1
    assert system.registry.status("k1316")["last_answer"] == "k1316 ready"
    assert system.registry.status("k1324")["initialized"] is False


def test_sync_broadcast_runs_rooms_concurrently_and_preserves_order() -> None:
    runtime = _runtime()
    probe = _ConcurrencyProbe()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _DelayedLLM(room_id, probe),
    )
    registry.register_all_rooms()
    manager = RoomAgentManagerApp(
        registry,
        max_parallel_room_agents=3,
    )

    result = manager.broadcast_to_rooms(
        ["k1316", "k1324", "k1315", "k1324"],
        "Inspect local room state.",
    )

    assert result["status"] == "ok", result
    assert [item["room_id"] for item in result["results"]] == [
        "k1316",
        "k1324",
        "k1315",
    ]
    assert probe.max_active == 3


def test_distinct_room_tasks_batch_runs_concurrently_and_rejects_duplicates() -> None:
    runtime = _runtime()
    probe = _ConcurrencyProbe()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _DelayedLLM(room_id, probe),
    )
    registry.register_all_rooms()
    manager = RoomAgentManagerApp(registry, max_parallel_room_agents=2)
    tasks = [
        {"room_id": "k1316", "task": "Check seminar readiness."},
        {"room_id": "k1324", "task": "Inspect office air quality."},
    ]

    result = manager.delegate_room_tasks(tasks)

    assert result["status"] == "ok"
    assert [item["room_id"] for item in result["results"]] == [
        item["room_id"] for item in tasks
    ]
    assert [registry.traces(item["room_id"])[-1]["trace"]["task"] for item in tasks] == [
        item["task"] for item in tasks
    ]
    assert probe.max_active == 2
    assert result["batch_wall_time_seconds"] > 0
    rejected = manager.delegate_room_tasks([tasks[0], tasks[0]])
    assert rejected["reason"] == "duplicate_room_id"
    assert registry.status("k1316")["run_count"] == 1


def test_async_broadcast_is_non_blocking_and_respects_concurrency_limit() -> None:
    runtime = _runtime()
    probe = _ConcurrencyProbe()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _DelayedLLM(room_id, probe),
    )
    registry.register_all_rooms()
    manager = RoomAgentManagerApp(
        registry,
        max_parallel_room_agents=2,
    )

    async def run_broadcast() -> dict:
        pending = asyncio.create_task(
            manager.broadcast_to_rooms_async(
                ["k1324", "k1316", "k1315"],
                "Inspect local room state.",
            )
        )
        await asyncio.sleep(0.01)
        assert not pending.done()
        return await pending

    result = asyncio.run(run_broadcast())

    assert result["status"] == "ok", result
    assert [item["room_id"] for item in result["results"]] == [
        "k1324",
        "k1316",
        "k1315",
    ]
    assert probe.max_active == 2


def test_concurrent_delegations_to_same_room_are_serialized() -> None:
    runtime = _runtime()
    probe = _ConcurrencyProbe()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _DelayedLLM(room_id, probe),
    )
    registry.register_all_rooms()

    async def run_twice() -> list[dict]:
        return list(
            await asyncio.gather(
                registry.delegate_async("k1324", "First task."),
                registry.delegate_async("k1324", "Second task."),
            )
        )

    results = asyncio.run(run_twice())

    assert [item["status"] for item in results] == ["ok", "ok"]
    assert probe.max_active == 1
    assert registry.status("k1324")["run_count"] == 2


def test_cumulative_metrics_survive_bounded_trace_eviction_and_restore() -> None:
    runtime = _runtime()
    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _ScriptedLLM(
            [_Message(content=f"{room_id} complete")]
        ),
        max_trace_runs=1,
    )
    registry.register_all_rooms()

    for index in range(3):
        result = registry.delegate("k1324", f"Room check {index}.")
        assert result["status"] == "ok"

    assert len(registry.traces("k1324")) == 1
    assert registry.status("k1324")["llm_turn_count_total"] == 3
    totals = registry.cumulative_metric_totals()
    assert totals["llm_call_count"] == 3
    assert totals["token_usage"]["total_tokens"] == 21

    snapshot = registry.snapshot()
    restored = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=lambda room_id: _ScriptedLLM(
            [_Message(content=f"{room_id} restored")]
        ),
        max_trace_runs=1,
    )
    restored.register_all_rooms()
    restored.restore(snapshot)

    assert restored.status("k1324")["llm_turn_count_total"] == 3
    assert restored.cumulative_metric_totals() == totals


def test_async_broadcast_isolates_one_room_failure() -> None:
    runtime = _runtime()
    probe = _ConcurrencyProbe()

    def make_llm(room_id: str):
        if room_id == "k1316":
            return _FailingLLM()
        return _DelayedLLM(room_id, probe)

    registry = RoomAgentRegistry(
        world=runtime.world,
        llm_factory=make_llm,
    )
    registry.register_all_rooms()
    manager = RoomAgentManagerApp(
        registry,
        max_parallel_room_agents=3,
    )

    result = asyncio.run(
        manager.broadcast_to_rooms_async(
            ["k1324", "k1316", "k1315"],
            "Inspect local room state.",
        )
    )

    assert result["status"] == "partial"
    assert [item["status"] for item in result["results"]] == [
        "ok",
        "error",
        "ok",
    ]
    assert "room provider unavailable" in result["results"][1]["reason"]
    assert registry.status("k1316")["failure_count"] == 1


def test_supervisor_auto_dispatches_new_room_events_before_llm_turn() -> None:
    runtime = _runtime()
    system = build_building_multi_agent_system(
        world=runtime.world,
        runtime=runtime,
        supervisor_llm=_ScriptedLLM([_Message(content="supervisor complete")]),
        room_llm_factory=lambda room_id: _ScriptedLLM(
            [_Message(content=f"{room_id} reconciled")]
        ),
    )
    event = runtime.world.publish_building_event(
        BuildingEventType.DEVICE_FAILED,
        source="monitor",
        subject_id="k1324_hvac_01",
    )
    system.registry.enqueue_event(
        "k1324",
        event,
        should_wake_agent=True,
        trigger_reason="device_failed",
    )

    result = system.run_supervisor("Continue building operations.")

    assert result["status"] == "ok", result
    assert system.registry.status("k1324")["run_count"] == 1
    assert system.registry.status("k1324")["pending_event_count"] == 0
    assert len(system.supervisor.auto_dispatch_history) == 1
    assert system.supervisor.auto_dispatch_history[0]["event_ids"] == [event.event_id]


def test_failed_auto_dispatch_is_not_retried_forever_in_same_supervisor_run() -> None:
    runtime = _runtime()
    failing_llms: list[_FailingLLM] = []

    def make_room_llm(_room_id: str):
        llm = _FailingLLM()
        failing_llms.append(llm)
        return llm

    supervisor_llm = _ScriptedLLM(
        [
            _Message(
                tool_calls=[_ToolCall("RoomAgentManagerApp__list_room_agents")]
            ),
            _Message(content="reported isolated room failure"),
        ]
    )
    system = build_building_multi_agent_system(
        world=runtime.world,
        runtime=runtime,
        supervisor_llm=supervisor_llm,
        room_llm_factory=make_room_llm,
    )
    event = runtime.world.publish_building_event(
        BuildingEventType.DEVICE_FAILED,
        source="monitor",
        subject_id="k1316_hvac_01",
    )
    system.registry.enqueue_event(
        "k1316",
        event,
        should_wake_agent=True,
        trigger_reason="device_failed",
    )

    result = system.run_supervisor("Continue despite isolated failures.")

    assert result["status"] == "ok", result
    assert len(system.supervisor.auto_dispatch_history) == 1
    assert system.registry.status("k1316")["run_count"] == 1
    assert system.registry.status("k1316")["pending_event_count"] == 1
    assert len(failing_llms) == 1


def test_combined_completion_guard_blocks_pending_room_events() -> None:
    runtime = _runtime()
    system = build_building_multi_agent_system(
        world=runtime.world,
        runtime=runtime,
        supervisor_llm=_ScriptedLLM([_Message(content="done")]),
        room_llm_factory=lambda room_id: _ScriptedLLM(
            [_Message(content=f"{room_id} reconciled")]
        ),
    )
    system.supervisor.completion_guard = lambda: {
        "status": "ok",
        "can_finish": True,
        "blockers": [],
    }
    engine = SimpleNamespace(
        agent=system.supervisor,
        time_manager=runtime.world.time_manager,
    )
    system.attach_engine(engine)
    system.attach_engine(engine)
    event = runtime.world.publish_building_event(
        BuildingEventType.MANUAL_OVERRIDE,
        source="operator",
        subject_id="k1315_hvac_01",
    )
    system.registry.enqueue_event(
        "k1315",
        event,
        should_wake_agent=True,
        trigger_reason="manual_override",
    )

    blocked = system.completion_status()
    assert blocked["can_finish"] is False
    assert blocked["blockers"][-1]["code"] == "room_agent_events_pending"

    delegated = system.registry.delegate("k1315", "Reconcile the pending event.")
    assert delegated["status"] == "ok"
    assert system.completion_status()["can_finish"] is True


def test_multi_agent_snapshot_restores_durable_state_and_rebuilds_lazily() -> None:
    runtime = _runtime()

    def build_system(active_runtime):
        return build_building_multi_agent_system(
            world=active_runtime.world,
            runtime=active_runtime,
            supervisor_llm=_ScriptedLLM([_Message(content="done")]),
            room_llm_factory=lambda room_id: _ScriptedLLM(
                [_Message(content=f"{room_id} restored")]
            ),
        )

    original = build_system(runtime)
    assert original.registry.delegate("k1324", "Initial check.")["status"] == "ok"
    runtime.advance_to(START_AT + timedelta(minutes=1))
    original.registry.set_enabled("k1316", False)
    event = runtime.world.publish_building_event(
        BuildingEventType.DEVICE_FAILED,
        source="monitor",
        subject_id="k1324_hvac_01",
    )
    original.registry.enqueue_event(
        "k1324",
        event,
        should_wake_agent=True,
        trigger_reason="device_failed",
    )
    pending_event_count = original.registry.status("k1324")[
        "pending_event_count"
    ]
    snapshot = original.snapshot()
    json.dumps(snapshot)

    restored_runtime = _runtime()
    restored = build_system(restored_runtime)
    restored.restore(snapshot)

    assert restored_runtime.current_time == runtime.current_time
    assert restored.registry.time_manager.time() == pytest.approx(
        runtime.current_time.timestamp(), abs=0.1
    )
    restored_status = restored.registry.status("k1324")
    assert restored_status["initialized"] is False
    assert restored_status["run_count"] == 1
    assert restored_status["pending_event_count"] == pending_event_count
    assert restored.registry.status("k1316")["enabled"] is False
    assert len(restored.registry.traces("k1324")) == 1

    resumed = restored.registry.delegate("k1324", "Resume after restore.")
    assert resumed["status"] == "ok"
    assert restored.registry.status("k1324")["run_count"] == 2
    assert restored.registry.status("k1324")["pending_event_count"] == 0
