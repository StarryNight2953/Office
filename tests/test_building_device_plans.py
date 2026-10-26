"""V5 offline contracts: fewer model turns without weaker world/time guards."""

from datetime import timedelta

import pytest

from fairy.apps.building_world import BuildingOperationsApp
from fairy.apps.building_world.device_plans import canonical_goals
from fairy.apps.building_world.types import BuildingEventType
from fairy.controllers.building.multi_agent import (
    BuildingMultiAgentSystem,
    RoomAgentManagerApp,
)
from fairy.controllers.building.room_agents import RoomAgentRegistry
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES


class _NoModel:
    provider = "openai"
    model = "no-model-v5"

    def __init__(self):
        self.calls = 0

    def chat_completion(self, messages, tools=None):
        self.calls += 1
        raise AssertionError("deterministic device plan should not call a model")


def _setup(index=0):
    scenario = L3_SCENARIO_CLASSES[index]()
    scenario.setup()
    engine = Engine(None, scenario, virtual_time=True)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    operations.enable_fair_wait_policy()
    model = _NoModel()
    registry = RoomAgentRegistry(
        world=operations.world,
        building_apps=scenario.apps,
        time_manager=engine.time_manager,
        llm_factory=lambda _: model,
    )
    registry.register_all_rooms()
    return scenario, engine, operations, registry, model


def _goals():
    return [
        {
            "device_id": "k1324_hvac_01",
            "power_on": True,
            "mode": "cooling",
            "level": 1,
            "target_temperature_c": 23,
        },
        {"device_id": "k1324_ventilation_01", "power_on": True, "level": 1},
    ]


@pytest.mark.parametrize(
    "device,alias,canonical",
    [
        ("k1324_hvac_01", "cool", "cooling"),
        ("k1324_hvac_01", "heat", "heating"),
        ("k1324_ventilation_01", "on", "outdoor_air"),
        ("k1315_humidifier_01", "humidification", "on"),
    ],
)
def test_known_mode_aliases_are_copied_and_canonicalized(device, alias, canonical):
    _, _, op, _, _ = _setup()
    original = [{"device_id": device, "power_on": True, "mode": alias}]
    goals = canonical_goals(op.world, op.world.devices[device].room_id, original)
    assert goals[0]["mode"] == canonical
    assert original[0]["mode"] == alias


@pytest.mark.parametrize(
    "goal",
    [
        {"device_id": "k1324_hvac_01", "mode": "continuous"},
        {"device_id": "k1324_ventilation_01", "mode": "co2_control"},
        {"device_id": "k1316_projector_01", "mode": "default"},
        {"device_id": "k1324_hvac_01", "power_on": False, "level": 1},
        {"device_id": "k1324_hvac_01", "mode": "cooling", "level": 0},
        {"device_id": "k1324_hvac_01", "target_temperature_c": 100},
        {"device_id": "k1316_projector_01", "power_on": True, "level": 3},
    ],
)
def test_impossible_goals_fail_before_any_model_or_mutation(goal):
    _, _, op, registry, model = _setup()
    room = op.world.devices[goal["device_id"]].room_id
    before = len(op.world.events)
    result = registry.delegate(room, "Execute explicit target", [goal])
    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_device_expectations"
    assert model.calls == 0
    assert len(op.world.events) == before
    assert registry.status(room)["run_count"] == 0


def test_worker_executes_complete_plan_with_zero_model_and_full_tool_accounting():
    _, engine, op, registry, model = _setup()
    result = registry.delegate("k1324", "Configure both controls", _goals())
    assert result["status"] == "ok"
    assert result["resolution"]["execution"] == "guarded_device_plan"
    assert result["verification"]["can_finish"]
    assert result["llm_turn_count"] == model.calls == 0
    assert result["tool_call_count"] == 2
    assert result["token_usage"]["total_tokens"] == 0
    assert len(op.device_plan_actions) == 2
    assert all(a["actor"] == "room_controller" for a in op.device_plan_actions)
    assert [a["action_id"] for a in op.device_plan_actions] == [1, 2]
    ledger = registry.all_run_ledgers()["k1324"][0]
    assert (
        ledger["tool_call_count"] == ledger["metric_totals"]["tool_metric_count"] == 2
    )
    events = registry.traces("k1324")[0]["trace"]["runtime_events"]
    assert sum(e["event_type"] == "tool_call_end" for e in events) == 2
    assert events[-1]["event_type"] == "agent_run_end"
    assert engine.time_manager.time() == op.runtime.current_time.timestamp()


def test_single_batch_and_worker_have_identical_command_effects():
    _, _, single, _, _ = _setup()
    _, _, multi, registry, model = _setup()
    assert single.apply_device_plan("k1324", _goals())["status"] == "ok"
    assert registry.delegate("k1324", "Configure controls", _goals())["status"] == "ok"
    for goal in _goals():
        assert (
            single.world.device_states[goal["device_id"]]
            == multi.world.device_states[goal["device_id"]]
        )
    assert model.calls == 0


def test_lighting_plan_preserves_unrequested_brightness_and_color():
    _, _, op, _, _ = _setup()
    device = "k1315_lighting_front_01"
    state = op.world.device_states[device]
    state.power_on, state.level, state.mode = True, 3, "default"
    state.settings.update(brightness_pct=70, color_temperature_k=3800, scene="default")
    result = op.apply_device_plan("k1315", [{"device_id": device, "power_on": True, "mode": "presentation"}])
    assert result["status"] == "ok" and state.mode == "presentation"
    assert state.settings["brightness_pct"] == 70
    assert state.settings["color_temperature_k"] == 3800


def test_controller_plan_honors_worker_tool_budget():
    from dataclasses import replace

    _, _, op, registry, model = _setup()
    session = registry._session("k1324")
    session.definition = replace(session.definition, max_tool_calls=1)
    result = registry.delegate("k1324", "Configure two controls", _goals())
    assert result["status"] == "stopped"
    assert result["reason"] == "max_tool_calls=1"
    assert result["tool_call_count"] == len(op.device_plan_actions) == 1
    assert model.calls == 0


def test_partial_closeout_hands_back_blockers_without_orphan_tool_model_request():
    _, engine, op, registry, model = _setup()
    op.runtime.current_time = op.operational_end_at
    engine.time_manager.set_offset(op.operational_end_at.timestamp() - engine.time_manager.start_time)
    op.world.room_states["k1316"].occupancy_count = 0
    for device in ("k1316_hvac_01", "k1316_projector_01"):
        op.world.device_states[device].power_on = True
    first = registry.delegate("k1316", "Close HVAC only", [{"device_id": "k1316_hvac_01", "power_on": False}])
    assert first["status"] == "blocked" and first["reason"] == "room_closeout_incomplete"
    assert first["tool_call_count"] == 1 and model.calls == 0
    assert any(b["code"] == "room_devices_still_on" for b in first["verification"]["blockers"])
    second = registry.delegate("k1316", "Close remaining device", [{"device_id": "k1316_projector_01", "power_on": False}])
    assert second["status"] == "ok" and model.calls == 0


def test_horizon_cannot_start_a_new_device_plan():
    _, _, op, registry, model = _setup()
    op.runtime.current_time = op.operational_end_at
    result = registry.delegate("k1324", "Start HVAC after end", [_goals()[0]])
    assert result["status"] == "blocked" and result["reason"] == "closeout_requires_power_off"
    assert model.calls == 0 and not op.device_plan_actions


def test_active_meeting_cannot_be_prematurely_shut_down_at_1115():
    _, engine, op, registry, model = _setup(3)
    meeting = op.world.schedule["morning-seminar"]
    op.runtime.current_time = meeting.start_at + timedelta(minutes=75)
    engine.time_manager.set_offset(
        op.runtime.current_time.timestamp() - engine.time_manager.start_time
    )
    op.world.room_states[meeting.room_id].occupancy_count = 7
    for item in op.get_meeting_readiness(meeting.meeting_id)["checklist"]:
        op.world.device_states[item["device_id"]].power_on = True
    ready = op.get_meeting_readiness(meeting.meeting_id)
    assert ready["phase"] == "active"
    assert ready["minutes_until_end"] == 15
    assert ready["end_at"] == meeting.end_at.isoformat()
    for device in ["k1316_hvac_01", "k1316_projector_01"]:
        result = registry.delegate(
            "k1316",
            "Meeting has ended, shut down",
            [{"device_id": device, "power_on": False}],
        )
        assert result["status"] == "blocked"
        assert result["reason"] == "meeting_equipment_required"
        assert op.world.device_states[device].power_on
    assert model.calls == 0
    commitments = op.get_commitment_status()
    current = next(
        x
        for x in commitments["commitments"]
        if x.get("meeting_id") == meeting.meeting_id
    )
    assert current["phase"] == "active" and not current["urgent"]


def test_compatible_same_room_tasks_merge_into_one_verified_worker_run():
    _, _, op, registry, model = _setup()
    manager = RoomAgentManagerApp(registry)
    result = manager.delegate_room_tasks(
        [
            {"room_id": "k1324", "task": "HVAC", "expected_devices": [_goals()[0]]},
            {
                "room_id": "k1324",
                "task": "Ventilation",
                "expected_devices": [_goals()[1]],
            },
        ]
    )
    assert result["status"] == "ok"
    assert result["room_count"] == 1 and result["coalesced_task_count"] == 1
    assert registry.status("k1324")["run_count"] == 1
    assert len(op.device_plan_actions) == 2 and model.calls == 0


def test_conflicting_batch_goals_rejected_before_any_room_runs():
    _, _, op, registry, _ = _setup()
    manager = RoomAgentManagerApp(registry)
    before = len(op.world.events)
    result = manager.delegate_room_tasks(
        [
            {
                "room_id": "k1324",
                "task": "on",
                "expected_devices": [{"device_id": "k1324_hvac_01", "power_on": True}],
            },
            {
                "room_id": "k1324",
                "task": "off",
                "expected_devices": [{"device_id": "k1324_hvac_01", "power_on": False}],
            },
        ]
    )
    assert result["status"] == "rejected"
    assert registry.status("k1324")["run_count"] == 0
    assert len(op.world.events) == before


def test_failed_plan_does_not_begin_approved_wait(monkeypatch):
    _, _, op, _, _ = _setup()
    monkeypatch.setattr(
        op, "guarded_wait", lambda _: pytest.fail("must not wait after failed plan")
    )
    result = op.apply_room_plans(
        [
            {
                "room_id": "k1324",
                "task": "underspecified",
                "expected_devices": [{"device_id": "k1324_hvac_01", "mode": "cooling"}],
            }
        ],
        wait_minutes=30,
    )
    assert result["status"] == "blocked" and result["wait"] is None


def test_unknown_or_foreign_plan_never_operates_another_room():
    _, _, op, _, _ = _setup()
    assert op.apply_device_plan("k1315", _goals())["status"] == "rejected"
    assert op.apply_device_plan({}, _goals())["status"] == "rejected"
    assert op.device_plan_actions == []


def _idle_event(op, room="k1324", kind="occupancy_changed"):
    return {
        "event_id": "test-idle-event",
        "event_type": kind,
        "subject_id": room,
        "payload": {"room_id": room, "occupancy_count": 0},
    }


def _interruption(event):
    return {
        "interruptions": [
            {"reason": "building_event_requires_reconciliation", "events": [event]}
        ]
    }


def test_routine_wait_acknowledgement_never_discards_unrelated_pending_work():
    from types import SimpleNamespace

    _, _, op, registry, _ = _setup()
    first = op.world.publish_building_event(
        BuildingEventType.OCCUPANCY_CHANGED,
        source="test",
        subject_id="k1324",
        payload={"room_id": "k1324"},
    )
    second = op.world.publish_building_event(
        BuildingEventType.MANUAL_OVERRIDE, source="test", subject_id="k1315"
    )
    registry.enqueue_event(
        "k1324", first, should_wake_agent=True, trigger_reason="test"
    )
    registry.enqueue_event(
        "k1315", second, should_wake_agent=True, trigger_reason="test"
    )
    owner = SimpleNamespace(registry=registry)
    assert not BuildingMultiAgentSystem._acknowledge_routine_wait_events(
        owner, {first.event_id}
    )
    assert not registry.pending_events("k1324")
    assert registry.pending_events("k1315")[0]["event_id"] == second.event_id


@pytest.mark.parametrize(
    "kind",
    ["manual_override", "meeting_started", "device_failed", "print_request_added"],
)
def test_meaningful_events_cannot_be_suppressed(kind):
    _, _, op, _, _ = _setup()
    assert (
        op._reconcile_routine_wait_events(_interruption(_idle_event(op, kind=kind)))
        is None
    )


def test_approved_wait_continues_only_past_verified_idle_event(monkeypatch):
    _, engine, op, _, _ = _setup()
    for device in op.world.device_states.values():
        device.power_on = False
    calls = []
    stable = {"occupied_environment_stable": True, "rooms": []}
    monkeypatch.setattr(op, "get_environment_control_status", lambda: stable)
    monkeypatch.setattr(op, "_monitor_urgent_commitments", list)
    monkeypatch.setattr(op, "_next_preparation_boundary_seconds", lambda: None)

    def advance(*, seconds, stop_on_event):
        assert stop_on_event
        calls.append(seconds)
        op.runtime.current_time += timedelta(seconds=seconds)
        engine.time_manager.add_offset(seconds)
        return {
            "status": "ok",
            "advanced_seconds": seconds,
            "interrupted": len(calls) == 1,
            **(
                _interruption(_idle_event(op))
                if len(calls) == 1
                else {"interruptions": []}
            ),
        }

    monkeypatch.setattr(op.system, "_advance_seconds", advance)
    result = op.guarded_wait(900)
    assert result["advanced_seconds"] == 900
    assert result["reason"] == "time_budget_reached"
    assert result["bounded_wait"]["reconciled_event_ids"] == ["test-idle-event"]
    assert len(calls) == 3 and max(calls) <= 300


def test_device_plan_tool_schema_preserves_nested_goal_objects():
    _, _, op, _, _ = _setup()
    tool = next(t for t in op.get_tools() if t.name == "apply_room_plans")
    schema = tool.to_open_ai()["function"]["parameters"]["properties"]
    goal = schema["room_tasks"]["items"]["properties"]["expected_devices"]["items"]
    assert goal["type"] == "object"
    assert goal["properties"]["power_on"]["type"] == "boolean"
    assert "Device-specific" in goal["properties"]["mode"]["description"]


def test_only_explicit_verified_plan_can_continue_occupied_room_event():
    _, _, op, _, _ = _setup()
    assert op.apply_device_plan("k1324", _goals())["status"] == "ok"
    op.world.room_states["k1324"].occupancy_count = 4
    event = _idle_event(op)
    event["payload"]["occupancy_count"] = 4
    readings = {
        "rooms": [
            {"room_id": "k1324", "stable": True, "warnings": [], "violations": []}
        ]
    }
    interruption = _interruption(event)
    assert op._reconcile_routine_wait_events(interruption, environment=readings) is None
    assert op._reconcile_routine_wait_events(
        interruption, {"k1324": _goals()}, readings
    ) == [event["event_id"]]
    op.world.device_states["k1324_ventilation_01"].power_on = False
    assert (
        op._reconcile_routine_wait_events(interruption, {"k1324": _goals()}, readings)
        is None
    )


def test_approved_plan_cannot_hide_new_environment_warning():
    _, _, op, _, _ = _setup()
    op.apply_device_plan("k1324", _goals())
    op.world.room_states["k1324"].occupancy_count = 4
    readings = {
        "rooms": [
            {"room_id": "k1324", "stable": False, "warnings": ["co2"], "violations": []}
        ]
    }
    assert (
        op._reconcile_routine_wait_events(
            _interruption(_idle_event(op)), {"k1324": _goals()}, readings
        )
        is None
    )


def test_gateway_exception_retains_failed_primitive_action(monkeypatch):
    _, _, op, _, _ = _setup()

    def unavailable(**kwargs):
        raise OSError("test gateway unavailable")

    monkeypatch.setitem(op.device_command_tools, "HvacApp__set_hvac", unavailable)
    result = op.apply_device_plan("k1324", [_goals()[0]])
    assert result["status"] == "blocked"
    assert result["reason"] == "device_plan_command_failed"
    assert len(op.device_plan_actions) == 1
    assert op.device_plan_actions[0]["result"]["error_type"] == "OSError"


def test_already_met_but_forbidden_goal_has_same_policy_in_single_and_worker():
    _, _, single, _, _ = _setup()
    _, _, multi, registry, _ = _setup()
    goals = [{"device_id": "k1324_ventilation_01", "power_on": False}]
    for op in (single, multi):
        op.world.room_states["k1324"].occupancy_count = 4
        op.world.device_states["k1324_ventilation_01"].power_on = False
    result = single.apply_device_plan("k1324", goals)
    worker = registry.delegate("k1324", "Do not ventilate", goals)
    assert result["status"] == worker["status"] == "blocked"
    assert result["reason"] == worker["reason"] == "meeting_ventilation_required"
    assert not single.device_plan_actions and not multi.device_plan_actions


def test_complete_coalesced_plan_verifies_before_one_approved_wait(monkeypatch):
    _, _, op, _, _ = _setup()
    waits = []

    def wait(seconds, *, approved_room_goals):
        assert all(op.world.device_states[g["device_id"]].power_on for g in _goals())
        waits.append((seconds, approved_room_goals))
        return {"reason": "time_budget_reached"}

    monkeypatch.setattr(op, "guarded_wait", wait)
    result = op.apply_room_plans(
        [
            {"room_id": "k1324", "task": "HVAC", "expected_devices": [_goals()[0]]},
            {
                "room_id": "k1324",
                "task": "Ventilation",
                "expected_devices": [_goals()[1]],
            },
        ],
        wait_minutes=20,
    )
    assert result["status"] == "ok" and result["coalesced_task_count"] == 1
    assert len(waits) == 1 and waits[0][0] == 1200
    assert len(waits[0][1]["k1324"]) == 2


@pytest.mark.parametrize("minutes", [-1, 61, True])
def test_plan_cannot_approve_unbounded_or_malformed_wait(minutes):
    _, _, op, _, _ = _setup()
    result = op.apply_room_plans(
        [{"room_id": "k1324", "task": "controls", "expected_devices": _goals()}],
        wait_minutes=minutes,
    )
    assert result["status"] == "rejected" and not op.device_plan_actions


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_cli_fair_mode_exports_primitive_action_audit_without_model(
    monkeypatch, tmp_path, mode
):
    import json
    import sys

    from fairy import cli
    from fairy.agents.llm.base_llm import BaseLLM

    model = _NoModel()
    monkeypatch.setattr(BaseLLM, "llm_builder", lambda _: model)
    monkeypatch.setattr(cli, "_cleanup_repo_side_exports", lambda: None)

    def scripted_run(engine, **kwargs):
        result = engine.scenario.get_typed_app(BuildingOperationsApp).apply_device_plan(
            "k1324", _goals()
        )
        assert result["status"] == "ok"
        engine.agent.stop_reason = "no_progress"
        return engine.agent.workflow

    monkeypatch.setattr(Engine, "run_scenario_agent", scripted_run)
    scenario_id = L3_SCENARIO_CLASSES[0]().scenario_id
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fairy.cli",
            "--scenario",
            scenario_id,
            "--agent",
            "--controller",
            "building_baseline_react",
            "--building-agent-mode",
            mode,
            "--fair-building-eval",
            "--provider",
            "vllm",
            "--model",
            model.model,
            "--output-dir",
            str(tmp_path),
        ],
    )
    cli.main()
    report = json.loads(
        (tmp_path / f"{scenario_id}.run_report.json").read_text(encoding="utf-8")
    )
    assert report["limits"]["fair_building_protocol"] == "building_l3_fair_ab_v5"
    assert report["metrics"]["device_plan_action_count"] == 2
    assert report["controller_telemetry"]["device_plan_action_count"] == 2
    actions = json.loads(
        (tmp_path / f"{scenario_id}.device_plan_actions.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(actions) == 2 and model.calls == 0
