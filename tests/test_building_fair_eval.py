"""Offline contracts for opt-in, mode-symmetric Building L3 rules."""

from types import SimpleNamespace

from fairy.agents.agent.agent import Agent
from fairy.apps.building_world import (
    BuildingOperationsApp,
    BuildingWorldApp,
    VentilationApp,
)
from fairy.apps.building_world.types import DeviceType
from fairy.controllers.building.fair_eval import install_single_agent_room_preflight
from fairy.controllers.building.room_agents import RoomScopedToolset
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES
from fairy.time_manager import TimeManager


def test_explicit_clock_ignores_wall_latency_but_honors_advance(monkeypatch) -> None:
    clock = TimeManager(include_wall_time=False)
    clock.reset(start_time=1_000.0)
    monkeypatch.setattr("fairy.time_manager.time.time", lambda: 9_999.0)

    assert clock.time() == 1_000.0
    clock.add_offset(300.0)
    assert clock.time() == 1_300.0


def test_single_and_room_worker_reject_same_idle_device_command() -> None:
    scenario = L3_SCENARIO_CLASSES[16]()  # L3 scenario 17; list is zero-indexed.
    scenario.setup()
    engine = Engine(None, scenario, virtual_time=True)
    operations = scenario.get_typed_app(BuildingOperationsApp)
    world = scenario.get_typed_app(BuildingWorldApp)
    ventilation = scenario.get_typed_app(VentilationApp)
    tool_name = "VentilationApp__set_ventilation"
    candidate = next(
        device_id
        for device_id, spec in world.devices.items()
        if spec.device_type == DeviceType.VENTILATION
        and operations.preflight_room_device_command(
            tool_name, {"device_id": device_id, "power_on": True, "level": 1}
        )
        and operations.preflight_room_device_command(
            tool_name, {"device_id": device_id, "power_on": True, "level": 1}
        )["reason"]
        == "unoccupied_energy_risk"
    )
    agent = Agent(
        name="single",
        llm=SimpleNamespace(provider="openai", model="fake"),
        toolsets=[ventilation],
    )
    schemas_before = list(agent.tool_schemas)
    install_single_agent_room_preflight(agent, operations)
    assert agent.tool_schemas == schemas_before

    call = {"device_id": candidate, "power_on": True, "level": 1}
    single_result = agent.tools_map[tool_name](**call)
    proxy = RoomScopedToolset(
        world=world,
        room_id=world.devices[candidate].room_id,
        apps=[ventilation, operations],
    )
    room_tool = next(
        tool
        for tool in proxy.get_tools()
        if tool.to_open_ai()["function"]["name"] == tool_name
    )
    room_result = room_tool(**call)

    assert engine.time_manager.time() == scenario.start_time
    assert single_result == room_result
    assert single_result["reason"] == "unoccupied_energy_risk"
