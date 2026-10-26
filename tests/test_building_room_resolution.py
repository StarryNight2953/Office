"""V4 controls: real scoped commands, policy handback, and durable accounting."""

import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from fairy.apps.building_world import BuildingOperationsApp
from fairy.apps.building_world.types import BuildingEventType
from fairy.controllers.building.room_agents import RoomAgentRegistry
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES


class _LLM:
    provider = "openai"
    model = "scripted-v4"

    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = 0

    def chat_completion(self, messages, tools=None):
        self.calls += 1
        if not self.messages:
            raise AssertionError(
                "unnecessary model call after deterministic completion"
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self.messages.pop(0))],
            usage=SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=2,
                total_tokens=7,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
        )


def _answer(text="done"):
    return SimpleNamespace(content=text, tool_calls=None)


def _command(name, arguments):
    return SimpleNamespace(
        content=None,
        tool_calls=[
            SimpleNamespace(
                type="function",
                id=f"call-{name}",
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
            )
        ],
    )


def _setup(index=0, messages=()):
    scenario = L3_SCENARIO_CLASSES[index]()
    scenario.setup()
    engine = Engine(None, scenario, virtual_time=True)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    model = _LLM(messages)
    registry = RoomAgentRegistry(
        world=operations.world,
        building_apps=scenario.apps,
        time_manager=engine.time_manager,
        llm_factory=lambda _: model,
    )
    registry.register_all_rooms()
    return scenario, engine, operations, registry, model


def _event(registry, event_type=BuildingEventType.MANUAL_OVERRIDE, room="k1324"):
    event = registry.world.publish_building_event(
        event_type, source="test", subject_id=room
    )
    registry.enqueue_event(room, event, should_wake_agent=True, trigger_reason="test")
    return event


def test_goal_already_met_finishes_without_model_and_acknowledges_events():
    _, _, _, registry, model = _setup()
    for state in registry.world.device_states.values():
        state.power_on = False
    event = _event(registry)
    result = registry.delegate(
        "k1324",
        "Ensure HVAC is off.",
        [{"device_id": "k1324_hvac_01", "power_on": False}],
    )
    assert result["status"] == "ok"
    assert result["reason"] == "device_postconditions_verified"
    assert model.calls == result["llm_turn_count"] == 0
    assert result["processed_event_ids"] == [event.event_id]
    assert (
        registry.all_run_ledgers()["k1324"][0]["metric_totals"]["token_usage"][
            "total_tokens"
        ]
        == 0
    )


def test_last_command_completes_at_tool_budget_without_extra_summary():
    message = _command(
        "HvacApp__set_hvac",
        {
            "device_id": "k1324_hvac_01",
            "power_on": False,
            "mode": "off",
            "target_temperature_c": 24,
            "fan_level": 0,
        },
    )
    _, _, operations, registry, model = _setup(messages=[message])
    operations.world.device_states["k1324_hvac_01"].power_on = True
    registry._session("k1324").definition = replace(
        registry._session("k1324").definition, max_tool_calls=1
    )
    result = registry.delegate(
        "k1324", "Turn HVAC off.", [{"device_id": "k1324_hvac_01", "power_on": False}]
    )
    assert result["status"] == "ok"
    assert model.calls == result["llm_turn_count"] == result["tool_call_count"] == 1
    assert registry.cumulative_metric_totals()["token_usage"]["total_tokens"] == 7
    assert (
        registry.traces("k1324")[0]["trace"]["runtime_events"][-1]["event_type"]
        == "agent_run_end"
    )


def test_resolution_waits_for_every_declared_device():
    _, _, operations, registry, model = _setup(
        messages=[
            _command(
                "HvacApp__set_hvac",
                {
                    "device_id": "k1324_hvac_01",
                    "power_on": False,
                    "mode": "off",
                    "target_temperature_c": 24,
                    "fan_level": 0,
                },
            ),
            _command(
                "VentilationApp__set_ventilation",
                {"device_id": "k1324_ventilation_01", "power_on": False, "level": 0},
            ),
        ]
    )
    operations.world.room_states["k1324"].occupancy_count = 0
    operations.world.device_states["k1324_hvac_01"].power_on = True
    operations.world.device_states["k1324_ventilation_01"].power_on = True
    result = registry.delegate(
        "k1324",
        "Shut down both.",
        [
            {"device_id": "k1324_hvac_01", "power_on": False},
            {"device_id": "k1324_ventilation_01", "power_on": False},
        ],
    )
    assert result["status"] == "ok"
    assert model.calls == 2
    assert result["verification"]["evidence"]["powered_device_ids"] == []


def test_blocked_ventilation_task_keeps_inbox_and_survives_reset():
    _, _, operations, registry, model = _setup()
    operations.world.room_states["k1324"].occupancy_count = 2
    operations.world.device_states["k1324_ventilation_01"].power_on = True
    event = _event(registry)
    goal = [{"device_id": "k1324_ventilation_01", "power_on": False}]
    progress_keys = []
    for _ in range(2):
        result = registry.delegate("k1324", "Turn ventilation off.", goal)
        assert result["status"] == "blocked"
        assert result["reason"] == "meeting_ventilation_required"
        assert result["processed_event_ids"] == []
        progress_keys.append(result["progress_key"])
        registry.reset("k1324", clear_inbox=False)
    assert model.calls == 0
    assert progress_keys[0] == progress_keys[1]
    assert registry.status("k1324")["run_count"] == 2
    assert registry.status("k1324")["reset_count"] == 2
    assert registry.pending_events("k1324")[0]["event_id"] == event.event_id


def test_early_preparation_defers_until_window_then_executes_once():
    scenario, engine, operations, registry, model = _setup(
        16,
        messages=[
            _command(
                "HvacApp__set_hvac",
                {
                    "device_id": "k1315_hvac_01",
                    "power_on": True,
                    "mode": "cooling",
                    "target_temperature_c": 22,
                    "fan_level": 2,
                },
            ),
        ],
    )
    meeting = operations.world.schedule["late-meeting"]
    now = meeting.start_at - timedelta(minutes=65)
    scenario.building_runtime.current_time = now
    engine.time_manager.set_offset(now.timestamp() - scenario.start_time)
    goal = [
        {
            "device_id": "k1315_hvac_01",
            "power_on": True,
            "mode": "cooling",
            "level": 2,
            "target_temperature_c": 22,
        }
    ]
    event = _event(registry, room="k1315")
    result = registry.delegate("k1315", "Prepare the room.", goal)
    assert result["status"] == "deferred"
    retry_at = meeting.start_at - timedelta(minutes=60)
    assert result["resolution"]["retry_at"] == retry_at.isoformat()
    assert result["pending_event_count"] == 1
    assert model.calls == 0
    scenario.building_runtime.current_time = retry_at
    engine.time_manager.set_offset(retry_at.timestamp() - scenario.start_time)
    result = registry.delegate("k1315", "Prepare the room.", goal)
    assert result["status"] == "ok"
    assert result["processed_event_ids"] == [event.event_id]
    assert model.calls == 1


def test_reset_and_trace_eviction_preserve_usage_ledger_and_checkpoint():
    _, _, _, registry, _ = _setup(messages=[_answer(), _answer(), _answer()])
    registry.max_trace_runs = 1
    for index in range(3):
        assert registry.delegate("k1324", f"Inspect room {index}")["status"] == "ok"
        registry.reset("k1324", clear_inbox=False)
    totals = registry.cumulative_metric_totals()
    assert totals["llm_call_count"] == 3
    assert totals["token_usage"]["total_tokens"] == 21
    assert len(registry.traces("k1324")) == 1
    assert len(registry.all_run_ledgers()["k1324"]) == 3
    assert [item["generation"] for item in registry.all_run_ledgers()["k1324"]] == [
        0,
        1,
        2,
    ]
    snapshot = registry.snapshot()
    registry.restore(snapshot)
    assert registry.cumulative_metric_totals() == totals
    assert registry.status("k1324")["run_count"] == 3
    assert registry.status("k1324")["reset_count"] == 3
    assert len(registry.all_run_ledgers()["k1324"]) == 3


def test_contract_can_shed_same_room_load_before_raising_hvac_level():
    messages = [
        _command(
            "AirDeviceApp__set_air_purifier",
            {"device_id": "k1324_purifier_01", "power_on": False, "level": 0},
        ),
        _command(
            "HvacApp__set_hvac",
            {
                "device_id": "k1324_hvac_01",
                "power_on": True,
                "mode": "cooling",
                "target_temperature_c": 22,
                "fan_level": 2,
            },
        ),
    ]
    _, _, operations, registry, model = _setup(messages=messages)
    world = operations.world
    for state in world.device_states.values():
        state.power_on = False
    world.room_states["k1324"].occupancy_count = 1
    hvac = world.device_states["k1324_hvac_01"]
    hvac.power_on, hvac.mode, hvac.level = True, "cooling", 1
    purifier = world.device_states["k1324_purifier_01"]
    purifier.power_on, purifier.level = True, 3
    operations.power_limit_w = (
        operations._estimated_device_power_w(
            world.devices["k1324_hvac_01"], replace(hvac, level=2)
        )
        + 1
    )
    result = registry.delegate(
        "k1324",
        "Shed purifier load, then increase HVAC.",
        [
            {"device_id": "k1324_purifier_01", "power_on": False},
            {
                "device_id": "k1324_hvac_01",
                "power_on": True,
                "mode": "cooling",
                "level": 2,
            },
        ],
    )
    assert result["status"] == "ok", result
    assert model.calls == 2
    assert operations.get_power_budget_status()["within_limit"] is True
    assert result["token_usage"]["total_tokens"] == 14


@pytest.mark.parametrize(
    "automatic,event_type,occupied,expected_calls",
    [
        (True, BuildingEventType.OCCUPANCY_CHANGED, 0, 0),
        (False, BuildingEventType.OCCUPANCY_CHANGED, 0, 1),
        (True, BuildingEventType.DEVICE_FAILED, 0, 1),
        (True, BuildingEventType.OCCUPANCY_CHANGED, 1, 1),
    ],
)
def test_empty_event_filter_is_scoped_and_does_not_hide_other_work(
    automatic, event_type, occupied, expected_calls
):
    _, _, operations, registry, model = _setup(messages=[_answer()])
    operations.world.schedule.clear()
    operations.world.room_states["k1324"].occupancy_count = occupied
    for state in operations.world.device_states.values():
        state.power_on = False
    event = _event(registry, event_type)
    result = registry.delegate(
        "k1324", "Reconcile events.", reconcile_events_only=automatic
    )
    assert result["status"] == "ok"
    assert result["processed_event_ids"] == [event.event_id]
    assert model.calls == expected_calls
