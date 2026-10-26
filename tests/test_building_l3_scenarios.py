"""Contract tests for the twenty Spec-driven Building L3 scenarios."""

import json
from types import SimpleNamespace

import pytest

from fairy.agents.agent.toolset_builder import build_toolset
from fairy.apps.building_world import (
    BuildingSensorApp,
    BuildingWorldApp,
    PrintingApp,
)
from fairy.apps.system import SystemApp
from fairy.controllers.building import (
    RoomScopedToolset,
    build_building_multi_agent_system,
    select_supervisor_toolsets,
)
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.l3.catalog import L3_SPECS
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES
from fairy.scenarios.registry import get_scenario_class, list_scenarios


class _ScriptedLLM:
    provider = "openai"
    model = "scripted-l3-multi-agent"

    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = list(messages)

    def chat_completion(self, messages, tools=None):
        message = (
            self.messages.pop(0)
            if self.messages
            else SimpleNamespace(content="done", tool_calls=None)
        )
        usage = SimpleNamespace(
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=7,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=usage,
        )


def _assistant_message(
    content: str | None = None,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
) -> SimpleNamespace:
    tool_calls = None
    if tool_name is not None:
        tool_calls = [
            SimpleNamespace(
                id=f"call_{tool_name}",
                type="function",
                function=SimpleNamespace(
                    name=tool_name,
                    arguments=json.dumps(tool_args or {}),
                ),
            )
        ]
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _replay(scenario_class):
    build_engine = Engine(None, scenario_class())
    oracle = build_engine.build_oracle_workflow(run_oracle=False)
    replay_engine = Engine(None, scenario_class())
    replayed = replay_engine.replay_workflow(oracle)
    return oracle, replayed, replay_engine.evaluation_report(replayed)


def test_l3_catalog_contains_twenty_distinct_valid_specs() -> None:
    assert len(L3_SPECS) == 20
    assert len({spec.scenario_id for spec in L3_SPECS}) == 20
    assert {spec.family for spec in L3_SPECS} == {
        "season",
        "occupancy",
        "interaction",
        "constraint",
    }
    assert all(len({phase.episode for phase in spec.phases}) >= 3 for spec in L3_SPECS)
    assert all(len(spec.primary_metrics) >= 3 for spec in L3_SPECS)
    assert all(
        spec.evaluation.max_occupied_comfort_violation_ratio < 1.0
        for spec in L3_SPECS
    )
    assert all(spec.preparation_lead_minutes >= 30 for spec in L3_SPECS)


def test_all_twenty_l3_scenarios_are_registered() -> None:
    registered = set(list_scenarios())
    assert len(L3_SCENARIO_CLASSES) == 20
    for spec, scenario_class in zip(L3_SPECS, L3_SCENARIO_CLASSES):
        assert spec.scenario_id in registered
        assert get_scenario_class(spec.scenario_id) is scenario_class


@pytest.mark.parametrize("scenario_class", L3_SCENARIO_CLASSES)
def test_multi_agent_roles_cover_every_l3_oracle_tool(scenario_class) -> None:
    scenario = scenario_class()
    scenario.setup()
    engine = Engine(None, scenario)
    world = scenario.get_typed_app(BuildingWorldApp)
    room_id = next(iter(world.rooms))

    _, supervisor_schemas, _ = build_toolset(
        [world, *select_supervisor_toolsets(scenario.apps or [])]
    )
    supervisor_tool_names = {
        item["function"]["name"] for item in supervisor_schemas
    }
    room_tool_names = {
        tool.to_open_ai()["function"]["name"]
        for tool in RoomScopedToolset(
            world=world,
            room_id=room_id,
            apps=scenario.apps or [],
        ).get_tools()
    }
    oracle = engine.build_oracle_workflow(run_oracle=False)
    required_tool_names = {
        step.tool_name
        for step in oracle.dag.values()
        if step.op_type == "TOOL" and step.tool_name
    }

    missing = required_tool_names - supervisor_tool_names - room_tool_names
    assert not missing, (
        f"{scenario.spec.scenario_id} has tools unavailable to both roles: "
        f"{sorted(missing)}"
    )
    assert "HvacApp__set_hvac" in room_tool_names
    assert "HvacApp__set_hvac" not in supervisor_tool_names
    assert "SystemApp__advance_time" in supervisor_tool_names


def test_multi_agent_consumes_l3_runtime_event_between_supervisor_turns() -> None:
    scenario = L3_SCENARIO_CLASSES[0]()
    scenario.setup()
    supervisor_llm = _ScriptedLLM(
        [
            _assistant_message(
                tool_name="SystemApp__advance_time",
                tool_args={"minutes": 60},
            ),
            _assistant_message(content="Event reconciliation completed."),
        ]
    )
    system = build_building_multi_agent_system(
        world=scenario.building_runtime.world,
        runtime=scenario.building_runtime,
        supervisor_llm=supervisor_llm,
        room_llm_factory=lambda room_id: _ScriptedLLM(
            [_assistant_message(content=f"{room_id} reconciled")]
        ),
        building_apps=scenario.apps or [],
        supervisor_toolsets=select_supervisor_toolsets(scenario.apps or []),
        max_supervisor_tool_calls=5,
    )
    engine = Engine(system.supervisor, scenario)
    system.attach_engine(engine)
    # This test isolates event-to-room scheduling. Scenario completion is
    # covered separately by the combined completion-guard tests.
    system.supervisor.completion_guard = None

    result = system.run_supervisor("Advance until the next event and reconcile it.")

    assert result["status"] == "ok", result
    assert result["tool_call_count"] == 1
    assert system.supervisor.auto_dispatch_history
    assert sum(
        item["run_count"] for item in system.registry.list_agents()
    ) >= 1
    assert system.registry.pending_room_ids() == ()


@pytest.mark.parametrize("scenario_class", L3_SCENARIO_CLASSES)
def test_each_l3_oracle_replays_and_passes_final_audit(scenario_class) -> None:
    oracle, replayed, report = _replay(scenario_class)
    metadata = report["validation"]["metadata"]

    assert report["validation"]["success"] is True
    assert len(oracle.dag) == len(replayed.dag)
    assert metadata["physics_steps"] > 0
    assert len(metadata["episodes"]) >= 3
    assert metadata["all_rooms_empty"] is True
    assert metadata["all_devices_off"] is True
    assert metadata["reservations_released"] is True
    assert metadata["print_jobs_complete"] is True
    assert metadata["interaction_seen"] is True
    assert metadata["power_violation_seconds"] == 0.0
    assert metadata["environment_within_limits"] is True
    assert metadata["unoccupied_energy_within_budget"] is True
    assert metadata["meetings_prepared"] is True
    assert metadata["print_deadlines_met"] is True
    # Assert sustained operating coverage directly instead of enforcing an
    # arbitrary total step count that rewards duplicate reads or padding.
    step_ids = set(oracle.dag)
    assert sum(step.startswith("reconcile_") for step in step_ids) >= 10
    assert sum(step.startswith("advance_to_checkpoint_") for step in step_ids) >= 8
    assert sum(step.startswith("observe_before_control_") for step in step_ids) >= 10


@pytest.mark.parametrize(
    "scenario_class",
    [
        L3_SCENARIO_CLASSES[0],
        L3_SCENARIO_CLASSES[4],
        L3_SCENARIO_CLASSES[10],
        L3_SCENARIO_CLASSES[15],
    ],
)
def test_family_anchors_include_reconcile_and_physical_verify_steps(
    scenario_class,
) -> None:
    oracle, _, _ = _replay(scenario_class)
    step_ids = set(oracle.dag)

    assert any(step.startswith("reconcile_") for step in step_ids)
    assert any(step.startswith("observe_before_control_") for step in step_ids)
    assert any(step.startswith("wait_for_response_") for step in step_ids)
    assert any(step.startswith("verify_response_") for step in step_ids)
    assert "verify_final_environment" in step_ids
    assert "report_l3_complete" in step_ids
    assert not any(step.startswith("final_shutdown_") for step in step_ids)


def test_dynamic_weather_profiles_change_during_the_day() -> None:
    for spec in L3_SPECS[:5]:
        provider = spec.outdoor.provider(spec.start_at)
        morning = provider(spec.start_at)
        afternoon = provider(spec.start_at + (spec.end_at - spec.start_at) / 2)
        assert (
            morning.air_temperature_c,
            morning.relative_humidity_pct,
            morning.pm25_ug_m3,
            morning.solar_irradiance_w_m2,
        ) != (
            afternoon.air_temperature_c,
            afternoon.relative_humidity_pct,
            afternoon.pm25_ug_m3,
            afternoon.solar_irradiance_w_m2,
        )


def test_interaction_briefings_do_not_leak_future_change_details() -> None:
    for scenario_class in L3_SCENARIO_CLASSES[10:15]:
        scenario = scenario_class()
        assert scenario.spec.interaction is not None
        assert scenario.spec.interaction.description not in scenario.scenario_input
        assert scenario.spec.title not in scenario.scenario_input
        assert scenario.spec.research_question not in scenario.scenario_input
        assert "届时已发生的事件" in scenario.scenario_input


def test_room_and_attendance_changes_are_not_applied_before_event() -> None:
    downsize = L3_SCENARIO_CLASSES[10]()
    downsize.setup()
    downsize_world = downsize.get_typed_app(BuildingWorldApp)
    assert downsize_world.schedule["downsized-meeting"].room_id == "k1315"
    assert downsize_world.schedule["downsized-meeting"].expected_attendees == 12
    assert "k1316" not in downsize.scenario_input

    upgrade = L3_SCENARIO_CLASSES[11]()
    upgrade.setup()
    upgrade_world = upgrade.get_typed_app(BuildingWorldApp)
    assert upgrade_world.schedule["upgraded-meeting"].room_id == "k1316"
    assert upgrade_world.schedule["upgraded-meeting"].expected_attendees == 6
    assert "k1315" not in upgrade.scenario_input


def test_initial_sensor_observations_exist_before_first_time_advance() -> None:
    scenario = L3_SCENARIO_CLASSES[0]()
    scenario.setup()
    Engine(None, scenario)
    readings = scenario.get_typed_app(BuildingSensorApp).read_all_sensors()["readings"]

    assert readings
    assert {item["quantity"] for item in readings} >= {
        "air_temperature_c",
        "relative_humidity_pct",
        "co2_ppm",
    }


def test_print_requirements_are_public_only_from_their_reveal_time() -> None:
    initial = L3_SCENARIO_CLASSES[7]()
    initial.setup()
    Engine(None, initial)
    initial_requests = initial.get_typed_app(PrintingApp).get_print_requests()[
        "requests"
    ]
    assert initial_requests == [
        {
            "request_id": "print-request-08-01",
            "document_name": "defense_materials",
            "copies": 18,
            "pages_per_copy": 3,
            "requested_by": "staff-01",
            "priority": 3,
            "submit_at": initial_requests[0]["submit_at"],
            "ready_by": initial_requests[0]["ready_by"],
            "status": "pending",
            "job_id": None,
        }
    ]

    future = L3_SCENARIO_CLASSES[12]()
    future.setup()
    Engine(None, future)
    printing = future.get_typed_app(PrintingApp)
    assert printing.get_print_requests()["requests"] == []
    future.building_runtime.advance_by(90 * 60)
    assert printing.get_print_requests()["requests"][0]["document_name"] == (
        "advanced_agenda"
    )


def test_agent_time_advance_stops_at_wake_worthy_building_event() -> None:
    scenario = L3_SCENARIO_CLASSES[0]()
    scenario.setup()
    Engine(None, scenario)
    system = scenario.get_typed_app(SystemApp)

    result = system.advance_time(minutes=60)

    assert result["interrupted"] is True
    assert result["advanced_seconds"] == pytest.approx(30 * 60, abs=1.0)
    assert result["interruptions"][0]["events"][0]["event_type"] == (
        "occupancy_changed"
    )


def test_tool_schemas_publish_domain_ranges_and_reject_extra_arguments() -> None:
    scenario = L3_SCENARIO_CLASSES[0]()
    scenario.setup()
    Engine(None, scenario)
    _, schemas, _ = build_toolset(list(scenario.apps or []))
    hvac_schema = next(
        item for item in schemas if item["function"]["name"] == "HvacApp__set_hvac"
    )["function"]["parameters"]

    assert hvac_schema["additionalProperties"] is False
    assert hvac_schema["properties"]["mode"]["enum"] == [
        "off",
        "cooling",
        "heating",
        "fan",
    ]
    assert hvac_schema["properties"]["target_temperature_c"]["minimum"] == 16.0
    assert hvac_schema["properties"]["fan_level"]["maximum"] == 3


def test_validator_reports_actionable_failed_invariants() -> None:
    scenario = L3_SCENARIO_CLASSES[7]()
    scenario.setup()
    Engine(None, scenario)

    result = scenario.validate()

    assert result.success is False
    assert "physics_evolved" in result.metadata["failed_invariants"]
    assert "meetings_complete" in result.metadata["failed_invariants"]
    assert result.metadata["failure_reasons"]
    assert result.metadata["print_deadline_results"][0]["reason"] == (
        "missing_or_non_unique_exact_job"
    )


@pytest.mark.parametrize(
    ("scenario_class", "initial_room", "target_room"),
    [
        (L3_SCENARIO_CLASSES[10], "k1315", "k1316"),
        (L3_SCENARIO_CLASSES[11], "k1316", "k1315"),
    ],
)
def test_room_change_is_hidden_until_due_then_patches_schedule(
    scenario_class, initial_room, target_room
) -> None:
    scenario = scenario_class()
    scenario.setup()
    world = scenario.get_typed_app(BuildingWorldApp)
    meeting_id = scenario.spec.meetings[0].activity_id

    assert world.schedule[meeting_id].room_id == initial_room
    scenario.building_runtime.advance_by(120 * 60)
    assert world.schedule[meeting_id].room_id == target_room
    assert world.reservations[f"{meeting_id}-reservation"].resource_id == target_room


def test_schedule_change_precedes_same_minute_preparation_event() -> None:
    scenario = L3_SCENARIO_CLASSES[12]()
    scenario.setup()
    world = scenario.get_typed_app(BuildingWorldApp)

    scenario.building_runtime.advance_by(90 * 60)
    relevant = [
        event.event_type.value
        for event in world.events
        if event.subject_id == "advanced-meeting"
    ]

    assert relevant.index("schedule_changed") < relevant.index(
        "meeting_preparation_due"
    )


def test_print_coordination_serializes_same_time_batches_by_priority() -> None:
    scenario_class = L3_SCENARIO_CLASSES[17]
    build_engine = Engine(None, scenario_class())
    oracle = build_engine.build_oracle_workflow(run_oracle=False)
    replay_engine = Engine(None, scenario_class())
    replayed = replay_engine.replay_workflow(oracle)
    printing = replay_engine.scenario.get_typed_app(PrintingApp)

    jobs = list(printing.jobs.values())
    assert [job["document_name"] for job in jobs] == [
        "seminar_notes",
        "conference_pack",
        "nameplates",
    ]
    assert [job["priority"] for job in jobs] == [3, 2, 1]
    assert jobs[2]["started_at_timestamp"] >= jobs[1]["ready_at_timestamp"]
    assert replay_engine.evaluation_report(replayed)["validation"]["success"] is True
