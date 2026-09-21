"""Regression tests for guarded and tool-based Agent completion."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from fairy.agents.agent.agent import Agent
from fairy.apps.agent_user_interface import AgentUserInterface
from fairy.apps.building_world import BuildingOperationsApp
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.l1.generated.scenario_building_kechuang_l1_power_constrained_adjustment import (
    ScenarioBuildingKechuangL1PowerConstrainedAdjustment,
)
from fairy.scenarios.building_kechuang.l1.manifest import (
    accepted_contracts,
    load_manifest,
)
from fairy.time_manager import TimeManager


class _FunctionCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    type = "function"

    def __init__(self, name: str, arguments: dict) -> None:
        self.id = f"call_{name}"
        self.function = _FunctionCall(name, json.dumps(arguments))


class _Message:
    def __init__(self, content: str | None = None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


def _agent(*, toolsets=None) -> Agent:
    return Agent(
        "completion-test",
        SimpleNamespace(provider="openai", model="fake"),
        toolsets=toolsets or [],
    )


class AgentCompletionSemanticsTests(unittest.TestCase):
    def test_send_message_to_user_finishes_without_another_llm_turn(self) -> None:
        interface = AgentUserInterface()
        time_manager = TimeManager()
        time_manager.reset(start_time=0.0)
        interface.register_time_manager(time_manager)
        agent = _agent(toolsets=[interface])
        agent.set_time_manager(time_manager)
        messages = [
            _Message(
                tool_calls=[
                    _ToolCall(
                        "AgentUserInterface__send_message_to_user",
                        {"content": "task complete"},
                    )
                ]
            )
        ]
        agent._chat_completion_with_retries = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: messages.pop(0)
        )

        answer = agent.run("perform the task", max_tool_calls=5)

        self.assertEqual(answer, "task complete")
        self.assertEqual(agent.stop_reason, "agent_finished")
        self.assertEqual(agent.tool_call_count, 1)
        self.assertEqual(agent.llm_turn_count, 1)
        self.assertEqual(
            list(agent.workflow.dag.values())[-1].tool_name,
            "AgentUserInterface__send_message_to_user",
        )

    def test_guard_rejection_uses_controller_user_message(self) -> None:
        agent = _agent()
        responses = [
            _Message(content="premature", tool_calls=None),
            _Message(content="complete", tool_calls=None),
        ]
        requests: list[list[dict]] = []

        def scripted(messages, *_args, **_kwargs):
            requests.append(list(messages))
            return responses.pop(0)

        agent._chat_completion_with_retries = scripted  # type: ignore[method-assign]
        guard_results = iter(
            (
                {"can_finish": False, "blockers": [{"code": "pending"}]},
                {"can_finish": True, "blockers": []},
            )
        )
        agent.completion_guard = lambda: next(guard_results)

        answer = agent.run("operate", timeout_seconds=5)

        self.assertEqual(answer, "complete")
        self.assertEqual(requests[1][-1]["role"], "user")
        self.assertIn("[Controller notice]", requests[1][-1]["content"])
        self.assertEqual(
            [step.op_type for step in agent.workflow.dag.values()][-2:],
            ["CONTROLLER", "ASSISTANT"],
        )

    def test_manifest_l1_power_app_does_not_install_l3_completion_guard(self) -> None:
        contract = next(
            item
            for item in accepted_contracts(load_manifest())
            if item["behavior_family"] == "power_constrained_adjustment"
        )
        self.assertEqual(
            contract["scenario_id"],
            "scenario_building_kechuang_l1_power_constrained_adjustment",
        )
        scenario = ScenarioBuildingKechuangL1PowerConstrainedAdjustment()
        scenario.setup()
        operations = scenario.get_typed_app(BuildingOperationsApp)
        agent = _agent()

        Engine(agent, scenario)

        self.assertFalse(operations.completion_guard_enabled)
        self.assertIsNone(agent.completion_guard)


if __name__ == "__main__":
    unittest.main()
