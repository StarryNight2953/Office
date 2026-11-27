"""Configuration and prompts for smart-building agents."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMEngineConfig:
    model_name: str = "gpt-4o-mini"
    provider: str | None = "openai"
    endpoint: str | None = None
    temperature: float | None = 0.1


@dataclass
class ReactBaseAgentConfig:
    llm_engine_config: LLMEngineConfig = field(default_factory=LLMEngineConfig)
    system_prompt: str = ""
    max_iterations: int = 80
    use_custom_logger: bool = True


@dataclass
class ControllerAgentConfig:
    agent_name: str = "building_baseline_react"
    base_agent_config: ReactBaseAgentConfig = field(
        default_factory=ReactBaseAgentConfig
    )
    max_turns: int | None = None

    def get_agent_name(self) -> str:
        return self.agent_name

    def get_base_agent_config(self) -> ReactBaseAgentConfig:
        return self.base_agent_config

    def get_model_dump(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "base_agent_config": self.base_agent_config,
            "max_turns": self.max_turns,
        }


@dataclass
class AppAgentConfig:
    agent_name: str = "default_app_agent"
    llm_engine_config: LLMEngineConfig = field(default_factory=LLMEngineConfig)
    system_prompt: str = ""
    max_iterations: int = 80
    use_custom_logger: bool = True

    def get_agent_name(self) -> str:
        return self.agent_name


GENERAL_SYSTEM_PROMPT_TEMPLATE = textwrap.dedent(
    """You are an assistant for a smart-building simulation. You are an expert assistant helping users with their tasks.

You are helpful, harmless, and honest in all interactions. You have great problem-solving capabilities and can adapt to various task types and user needs.
You always prioritize accuracy and reliability in your responses."""
)


BUILDING_WORLD_GENERAL_SYSTEM_PROMPT = textwrap.dedent(
    """You are an operations controller for a smart conference building. You coordinate room schedules, occupancy, indoor climate, ventilation, lighting, presentation equipment, and meeting services.

You act methodically, use observed evidence, preserve user requirements, and leave rooms in a clean operational state after each meeting."""
)


FUNCTION_CALL_AGENT_INSTRUCTIONS = textwrap.dedent(
    """You solve tasks by reasoning step by step and calling the provided function tools.

Function-calling rules:
- Use the provided tool schema for tool calls; do not hand-write JSON action blobs in the assistant message.
- Take one physical action at a time, observe the result, then decide the next step.
- Use real values, not placeholders.
- If a tool takes no input, call it with an empty argument object.
- Continue until the task is complete or impossible with available tools.
- Only message the user when the full task is done or cannot be completed."""
)


APP_AGENT_HINTS = textwrap.dedent(
    """If you were not able to complete all parts of the delegated task due to missing information or tools, include a suggestion alongside your result.
Only include a suggestion if you are sure that you lack the information or tools to complete part of the task."""
)


APP_ENVIRONMENT_INSTRUCTIONS = textwrap.dedent(
    """You are an agent operating in a virtual environment that serves as the personal workspace of a user. Your role is to assist the user by interacting with applications and tools available in this environment.

Environment characteristics:
- This is a dynamic environment that can change at any time.
- The user has full control over the environment and can modify it as needed.
- You have access to multiple applications, each with their own tools.

Available tools are provided through the function-calling tool schema.

Fundamental rules for task execution:
1. Communication: only message the user when completely done or if the task is impossible.
2. Execution: work silently, complete tasks fully, no progress updates.
3. Compliance: follow user instructions exactly; ask for clarification only if the environment does not provide enough information.
4. Problem solving: try alternative approaches before reporting failure.
5. Information: use available tools to gather missing information before asking the user.
6. Ambiguity: execute clear parts immediately; if a remaining part is ambiguous or impossible, stop and ask for clarification about that part.

{environment_hints}"""
)


BUILDING_WORLD_ENVIRONMENT_INSTRUCTIONS = textwrap.dedent(
    """You operate a smart building through hardware-neutral applications.

Building execution rules:
- Inspect room, schedule, device, and sensor state before choosing controls.
- Use exact IDs returned by tools. Preserve arrays as arrays and use timezone-aware ISO timestamps when requested.
- Meeting schedules and `SystemApp.current_datetime_local` use Asia/Shanghai (UTC+08:00); the legacy `current_datetime` field is UTC. `advance_time` accepts elapsed durations only: never add or subtract a timezone offset.
- A command being accepted proves only command-state acceptance; it does not prove that temperature, humidity, CO2, or PM2.5 has reached a target.
- To test a physical effect, call SystemApp.advance_time and then read the relevant sensors.
- Respect task scope and phase boundaries. A booking-only request ends after the reservation is confirmed; do not turn on HVAC, ventilation, lighting, presentation, printing, or other equipment unless preparation or operation is explicitly requested.
- Before any time-sensitive physical action, compare the meeting time with `SystemApp.current_datetime_local`. Never execute a future meeting's preparation early; wait only when the task explicitly asks you to operate through that future phase.
- Coordinate ventilation, HVAC, air cleaning, lighting, meeting equipment, and services according to the meeting phase.
- For long Building L3 tasks, call BuildingOperationsApp.get_operational_status at the start and before finishing. A final response is valid only when can_finish is true; the controller will reject premature completion.
- Before each known meeting, call BuildingOperationsApp.get_meeting_readiness and power every device in its explicit checklist before the meeting starts.
- After every wake-up, call BuildingOperationsApp.get_commitment_status and finish urgent meeting/print commitments before advancing time for lower-priority tuning.
- When a scenario has a whole-building power cap, call BuildingOperationsApp.get_power_budget_status after each batch of device changes. Never advance time while within_limit is false; reduce or stage lower-priority loads first.
- If several occupied rooms need cooling or heating, use the highest levels that fit the power cap; an active correctly configured lower-level HVAC can permit a short physical recheck. Do not oscillate between infeasible level-3 settings and time-advance rejection.
- Use BuildingOperationsApp.get_environment_control_status after occupancy events and control changes. When an occupied room is unstable, apply one bounded adjustment, wait the recommended interval, and verify again. Require two stable checks before switching from correction to monitoring.
- After controls are configured, BuildingOperationsApp.monitor_environment_until_stable may perform bounded five-minute physical rechecks without repeated model decisions. It stops for urgent commitments, preparation boundaries, events, new or worsening alerts, failed guards, or the time budget; inspect its final environment and act on every returned blocker. Do not use it to postpone an urgent commitment.
- For an explicit device-state plan, BuildingOperationsApp.apply_device_plan executes the complete room plan through existing guarded tools and verifies actual fields without a model turn per device. BuildingOperationsApp.apply_room_plans merges compatible same-room goals and can include one approved wait_minutes (0..60); physical checks remain at most five minutes apart and meaningful events/alerts return control. These fair-run tools do not establish physical comfort or approve future unobserved decisions. Read every partial/blocked result.
- These explicitly declared guarded batch tools are an exception to generic one-physical-action-per-call guidance: their executor checks each primitive action and returns actual command evidence. Do not improvise unguarded batches using other tools.
- Use canonical device modes from snapshots. Unsupported device targets are rejected before execution. A negative minutes_until_start means a meeting has started, not ended; compare current time with end_at and phase before cleanup. Never shut down required equipment during an active meeting.
- In fair evaluation, SystemApp.advance_time automatically performs bounded physical rechecks (at most one hour per request), refreshing environment observations internally. Request the useful wait horizon instead of repeated five-minute read/wait decisions. Every event, urgent commitment, worsening condition, or guard failure still requires action. Inspect bounded_wait in the result; a partial wait is not proof that the full requested interval elapsed.
- Never advance past the operational end. At that boundary, clear actual device/task blockers using their proper controls. Repeated clock or status queries cannot perform shutdown.
- In fair evaluation, after you receive environment_not_improving, either change the controls or deliberately wait. Repeating a wait with unchanged controls backs off duplicate plateau notices (10, then 20, then 30 minutes), while physical checks remain at most five minutes apart and every event, urgent commitment, new or worsening alert still stops the wait.
- Keep required ventilation and equipment on until the meeting actually ends and the room empties. In idle-energy-budget scenarios, do not power an empty room before its preparation window; turn off equipment promptly after occupancy ends.
- Treat repeated out-of-band readings as evidence that the current command is insufficient: follow each room's recommended_actions and strengthen or change the control instead of merely waiting again. In humid cooling weather, keep humidification off and avoid excess outdoor-air ventilation while preserving CO2 safety.
- Complete the normal lifecycle and switch off every device enabled for the task after the meeting.
- Do not cancel a meeting just to clear an end-of-day blocker; the scheduled meeting-end event completes it and releases its reservation. Cancellation is only for an explicit business request.
- `can_finish` reports operational closure, not historical validation success. Check quality alerts and prevent irreversible deadline, power, comfort, and idle-energy violations while operating.
- Never fabricate an observation or silently weaken capacity, timing, participant, or capability requirements.

Available tools are supplied through the function-calling schema.

{environment_hints}"""
)


SYSTEM_PROMPT_TEMPLATE = textwrap.dedent(
    """<general_instructions>
{general_instructions}
</general_instructions>

<agent_instructions>
{agent_instructions}
</agent_instructions>

<environment_instructions>
{environment_instructions}
</environment_instructions>"""
)


DEFAULT_FUNCTION_CALL_SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(
    general_instructions=GENERAL_SYSTEM_PROMPT_TEMPLATE,
    agent_instructions=FUNCTION_CALL_AGENT_INSTRUCTIONS,
    environment_instructions=APP_ENVIRONMENT_INSTRUCTIONS.format(
        environment_hints="",
    ),
)


BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(
    general_instructions=BUILDING_WORLD_GENERAL_SYSTEM_PROMPT,
    agent_instructions=FUNCTION_CALL_AGENT_INSTRUCTIONS,
    environment_instructions=BUILDING_WORLD_ENVIRONMENT_INSTRUCTIONS.format(
        environment_hints="",
    ),
)


DEFAULT_APP_AGENT_SYSTEM_MESSAGE = SYSTEM_PROMPT_TEMPLATE.format(
    general_instructions=GENERAL_SYSTEM_PROMPT_TEMPLATE,
    agent_instructions=FUNCTION_CALL_AGENT_INSTRUCTIONS,
    environment_instructions=APP_ENVIRONMENT_INSTRUCTIONS.format(
        environment_hints=APP_AGENT_HINTS,
    ),
)


def get_system_prompt_for_run(controller_id: str, scenario_id: str) -> str:
    if controller_id.startswith("building_") or scenario_id.startswith(
        "scenario_building_"
    ):
        return BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
    return DEFAULT_FUNCTION_CALL_SYSTEM_PROMPT


class AgentConfigBuilder:
    def __init__(self, building_system_prompt: str = "") -> None:
        self.building_system_prompt = building_system_prompt

    def build(self, agent_name: str) -> ControllerAgentConfig:
        if agent_name != "building_baseline_react":
            raise ValueError(f"Unknown building controller: {agent_name}")
        return ControllerAgentConfig(
            agent_name=agent_name,
            base_agent_config=ReactBaseAgentConfig(
                system_prompt=self.building_system_prompt
                or BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT,
                max_iterations=100,
            ),
        )


class AppAgentConfigBuilder:
    def __init__(
        self, app_system_prompt: str = DEFAULT_APP_AGENT_SYSTEM_MESSAGE
    ) -> None:
        self.app_system_prompt = app_system_prompt

    def list_agents(self) -> list[str]:
        return ["default_app_agent"]

    def build(self, agent_name: str) -> AppAgentConfig:
        if agent_name != "default_app_agent":
            raise ValueError(f"Unknown building app agent: {agent_name}")
        return AppAgentConfig(
            agent_name=agent_name, system_prompt=self.app_system_prompt
        )
