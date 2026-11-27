"""Construct building controllers and bounded application delegates."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from smartoffice.agents.agent.agent import Agent
from smartoffice.agents.agent.base_agent import CST
from smartoffice.controllers.agent_config import (
    BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT,
    AgentConfigBuilder,
    AppAgentConfig,
    AppAgentConfigBuilder,
    ControllerAgentConfig,
)
from smartoffice.controllers.app_agent import FinalAnswerApp

CONTROLLER_FAMILIES = ("building_baseline_react",)
CONTROLLER_HIDDEN_TOOL_NAMES = frozenset({"AgentUserInterface__send_message_to_agent"})


class ControllerAgent(Agent):
    """Building function-calling controller with per-run usage telemetry."""

    def __init__(
        self,
        name: str,
        llm: Any,
        system_message: str = BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT,
        messages: Any = None,
        toolsets: list[Any] | None = None,
    ) -> None:
        super().__init__(
            name=name,
            llm=llm,
            system_message=system_message,
            messages=messages,
            toolsets=toolsets,
            hidden_tool_names=CONTROLLER_HIDDEN_TOOL_NAMES,
        )
        self._reset_telemetry()

    def _reset_telemetry(self) -> None:
        self.telemetry: dict[str, Any] = {
            "family_id": self.name,
            "planning_mode": "react",
            "tool_calls": 0,
            "llm_calls": 0,
            "error_count": 0,
        }

    def after_llm_message(self, message: Any) -> None:
        self.telemetry["llm_calls"] += 1

    def after_tool_response(self, tool_call: Any, tool_response: Any) -> None:
        self.telemetry["tool_calls"] += 1

    def after_agent_error(self, exc: Exception) -> None:
        self.telemetry["error_count"] += 1

    def after_agent_run(self) -> None:
        self.record_event(
            "controller_telemetry", status="ok", telemetry=dict(self.telemetry)
        )

    def _ensure_current_time_system_prompt(self) -> None:
        if self.time_manager is None or not self.messages.messages:
            return
        first_message = self.messages.messages[0]
        if not isinstance(first_message, dict) or first_message.get("role") != "system":
            return
        content = str(first_message.get("content") or "")
        if "Today's date in 'YYYY-MM-DD HH' format is" in content:
            return
        date_str = datetime.fromtimestamp(self.time_manager.time(), tz=CST).strftime(
            "%Y-%m-%d %H"
        )
        first_message["content"] = (
            f"{content}\n\nToday's date in 'YYYY-MM-DD HH' format is {date_str}"
        )

    def run(self, input, **kwargs):
        self._reset_telemetry()
        self._ensure_current_time_system_prompt()
        return super().run(str(input), **kwargs)


class AgentBuilder:
    def list_agents(self) -> list[str]:
        return list(CONTROLLER_FAMILIES)

    def build(
        self,
        agent_config: ControllerAgentConfig,
        llm: Any,
        toolsets: list[Any] | None = None,
        system_message: str | None = None,
    ) -> ControllerAgent:
        family_id = agent_config.get_agent_name()
        if family_id not in CONTROLLER_FAMILIES:
            raise ValueError(f"Unknown building controller: {family_id}")
        return ControllerAgent(
            name=family_id,
            llm=llm,
            system_message=system_message
            or agent_config.base_agent_config.system_prompt
            or BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT,
            toolsets=toolsets,
        )


class AppAgentBuilder:
    def list_agents(self) -> list[str]:
        return AppAgentConfigBuilder().list_agents()

    def build(
        self,
        app_agent_config: AppAgentConfig,
        llm: Any,
        app: Any,
    ) -> Agent:
        return Agent(
            name=app_agent_config.agent_name,
            llm=llm,
            system_message=app_agent_config.system_prompt,
            toolsets=[app, FinalAnswerApp()],
        )


def build_controller_agent(
    family_id: str,
    llm: Any,
    toolsets: list[Any] | None = None,
    system_message: str | None = None,
) -> ControllerAgent:
    config = AgentConfigBuilder(building_system_prompt=system_message or "").build(
        family_id
    )
    return AgentBuilder().build(
        agent_config=config, llm=llm, toolsets=toolsets, system_message=system_message
    )
