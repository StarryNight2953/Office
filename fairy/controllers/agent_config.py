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
class ResearchSkillConfig:
    enabled: bool = False
    library_path: str | None = None
    top_k: int = 3
    min_score: float = 0.1


@dataclass
class ResearchMemoryConfig:
    enabled: bool = False
    max_items: int = 8
    injection_top_k: int = 3


@dataclass
class ResearchDelegationConfig:
    enabled: bool = False
    specialists: list[str] = field(default_factory=list)
    max_delegations_per_turn: int = 0


@dataclass
class ResearchVerificationConfig:
    enabled: bool = False
    uncertainty_keywords: list[str] = field(
        default_factory=lambda: ["maybe", "uncertain", "not sure", "risk", "verify"]
    )


@dataclass
class ResearchReWooConfig:
    enabled: bool = False
    max_plan_steps: int = 8
    max_replans: int = 1


@dataclass
class ResearchTreeSearchConfig:
    enabled: bool = False
    branch_factor: int = 3
    search_depth: int = 2
    max_expansions: int = 6


@dataclass
class ResearchCriticConfig:
    enabled: bool = False
    max_revision_cycles: int = 2
    enforce_precondition_checks: bool = True


@dataclass
class ResearchGraphMemoryConfig:
    enabled: bool = False
    max_nodes: int = 60
    retrieval_top_k: int = 5
    contradiction_check: bool = True


@dataclass
class ResearchTelemetryConfig:
    enabled: bool = True
    emit_to_result_metadata: bool = True


@dataclass
class ResearchAgentProfileConfig:
    family_id: str = "farm_baseline_react"
    planning_mode: str = "react"
    replan_on_tool_error: bool = False
    reflection: ResearchMemoryConfig = field(default_factory=ResearchMemoryConfig)
    skills: ResearchSkillConfig = field(default_factory=ResearchSkillConfig)
    delegation: ResearchDelegationConfig = field(
        default_factory=ResearchDelegationConfig
    )
    verification: ResearchVerificationConfig = field(
        default_factory=ResearchVerificationConfig
    )
    rewoo: ResearchReWooConfig = field(default_factory=ResearchReWooConfig)
    tree_search: ResearchTreeSearchConfig = field(
        default_factory=ResearchTreeSearchConfig
    )
    critic: ResearchCriticConfig = field(default_factory=ResearchCriticConfig)
    graph_memory: ResearchGraphMemoryConfig = field(
        default_factory=ResearchGraphMemoryConfig
    )
    telemetry: ResearchTelemetryConfig = field(default_factory=ResearchTelemetryConfig)

    @classmethod
    def for_family(cls, family_id: str) -> ResearchAgentProfileConfig:
        config = AgentConfigBuilder().build(family_id)
        if config.research_profile is None:
            return cls(family_id=family_id, planning_mode="react")
        return config.research_profile


@dataclass
class ControllerAgentConfig:
    agent_name: str = "farm_baseline_react"
    base_agent_config: ReactBaseAgentConfig = field(
        default_factory=ReactBaseAgentConfig
    )
    research_profile: ResearchAgentProfileConfig | None = None
    max_turns: int | None = None

    def get_agent_name(self) -> str:
        return self.agent_name

    def get_base_agent_config(self) -> ReactBaseAgentConfig:
        return self.base_agent_config

    def get_model_dump(self) -> dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "base_agent_config": self.base_agent_config,
            "research_profile": self.research_profile,
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
    """Your name is MetaOSSAgent, part of the Meta Agents Research Environments. You are an expert assistant helping users with their tasks.

You are helpful, harmless, and honest in all interactions. You have great problem-solving capabilities and can adapt to various task types and user needs.
You always prioritize accuracy and reliability in your responses."""
)

FARM_WORLD_GENERAL_SYSTEM_PROMPT = textwrap.dedent(
    """You are an experienced farm manager for a soybean farm in Harbin, Heilongjiang. You manage 64 ridges across a 268m x 71m field through the full growing season: field preparation, planting, crop monitoring, and harvest.

You think and act like a seasoned farmer: practical, methodical, and grounded in real agronomic knowledge."""
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

# ARE-compatible behavioral guidance adapted to native function calling.  The
# legacy text-JSON Action format, <end_action> marker, and embedded tool catalog
# are intentionally excluded because FAIRY supplies structured tool schemas.
ARE_FUNCTION_CALL_AGENT_INSTRUCTIONS = textwrap.dedent(
    """You are an expert assistant who solves tasks by reasoning step by step and calling the provided function tools.

You must always follow the cycle:
1. Thought: briefly explain what you are thinking and why a tool is needed.
2. Tool call: call exactly ONE tool through the provided function schema.
3. Observation: will be provided by the system; you NEVER generate or fabricate it.

=== THOUGHT RULES ===
- Always explain your reasoning in natural language before the tool call.
- Keep tool call details and arguments in the structured tool call, not in the Thought.

=== TOOL CALL RULES ===
- Use the provided tool schema for tool calls; do not hand-write JSON action blobs in the assistant message.
- Only ONE tool call is allowed per turn.
- Each tool call represents one physical action.
- Use real values, not placeholders.
- If a tool takes no input, call it with an empty argument object.
- For booleans, use true/false in lowercase.

=== OBSERVATION RULES ===
- Do NOT generate or fabricate the Observation; the system will provide the tool result after each call.

=== EXECUTION GUIDELINES ===
- Take one action at a time and complete the thought/tool-call/observation cycle before proceeding.
- If a tool call fails, analyze the error and try a different approach.
- Don't call tools unnecessarily - use your reasoning when you can solve something directly.
- Continue iterating until the task is complete or you determine it is impossible with available tools.
- Pay attention to tool outputs and use them to inform subsequent actions.
- Only message the user when the full task is done or cannot be completed."""
)

APP_AGENT_HINTS = textwrap.dedent(
    """If you were not able to complete all parts of the delegated task due to missing information or tools, include a suggestion alongside your result.
Only include a suggestion if you are sure that you lack the information or tools to complete part of the task."""
)

ARE_SIMULATION_ENVIRONMENT_INSTRUCTIONS = textwrap.dedent(
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

FARM_WORLD_ENVIRONMENT_INSTRUCTIONS = textwrap.dedent(
    """You are managing a soybean farm through a set of applications, each controlling a physical subsystem on the farm.

Available tools are provided through the function-calling tool schema.

Each tool call represents one physical action. Call one tool, observe the result, then decide the next step. Only message the user when the full task is done or cannot be completed.

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
    environment_instructions=ARE_SIMULATION_ENVIRONMENT_INSTRUCTIONS.format(
        environment_hints="",
    ),
)

FARM_WORLD_FUNCTION_CALL_SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(
    general_instructions=FARM_WORLD_GENERAL_SYSTEM_PROMPT,
    agent_instructions=FUNCTION_CALL_AGENT_INSTRUCTIONS,
    environment_instructions=FARM_WORLD_ENVIRONMENT_INSTRUCTIONS.format(
        environment_hints="",
    ),
)

FARM_WORLD_ARE_FUNCTION_CALL_SYSTEM_PROMPT = SYSTEM_PROMPT_TEMPLATE.format(
    general_instructions=FARM_WORLD_GENERAL_SYSTEM_PROMPT,
    agent_instructions=ARE_FUNCTION_CALL_AGENT_INSTRUCTIONS,
    environment_instructions=FARM_WORLD_ENVIRONMENT_INSTRUCTIONS.format(
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

FARM_SYSTEM_PROMPT_MODES = ("fairy", "are")


def get_farm_world_system_prompt(mode: str = "fairy") -> str:
    prompts = {
        "fairy": FARM_WORLD_FUNCTION_CALL_SYSTEM_PROMPT,
        "are": FARM_WORLD_ARE_FUNCTION_CALL_SYSTEM_PROMPT,
    }
    try:
        return prompts[mode]
    except KeyError as exc:
        choices = ", ".join(FARM_SYSTEM_PROMPT_MODES)
        raise ValueError(
            f"Unknown FARM system prompt mode {mode!r}; choose from: {choices}"
        ) from exc


def get_system_prompt_for_run(
    controller_id: str, scenario_id: str, farm_mode: str = "fairy"
) -> str:
    """Select a domain prompt from both controller and scenario identity."""

    if controller_id.startswith("building_") or scenario_id.startswith(
        "scenario_building_"
    ):
        return BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
    if controller_id.startswith("farm_") or scenario_id.startswith("scenario_farm_"):
        return get_farm_world_system_prompt(farm_mode)
    return DEFAULT_FUNCTION_CALL_SYSTEM_PROMPT


DEFAULT_APP_AGENT_SYSTEM_MESSAGE = SYSTEM_PROMPT_TEMPLATE.format(
    general_instructions=GENERAL_SYSTEM_PROMPT_TEMPLATE,
    agent_instructions=FUNCTION_CALL_AGENT_INSTRUCTIONS,
    environment_instructions=ARE_SIMULATION_ENVIRONMENT_INSTRUCTIONS.format(
        environment_hints=APP_AGENT_HINTS,
    ),
)


PLANNING_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Before calling tools, create a compact plan with explicit milestones.\n"
    "After each tool call, check whether the plan still holds; if not, replan before continuing.\n"
    "End by reporting completion status and unresolved risks.\n"
    "</research_family_instructions>"
)

REFLECTIVE_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Keep a short reflection loop: track mistakes, assumptions, and fixes across turns.\n"
    "Before deciding the next action, consult your memory notes and avoid repeated mistakes.\n"
    "</research_family_instructions>"
)

DELEGATION_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Use specialist reasoning modes (weather, soil, equipment, operations) before committing to action.\n"
    "Synthesize specialist viewpoints into one final executable action plan.\n"
    "</research_family_instructions>"
)

VERIFIER_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Use adaptive compute: short plan for straightforward cases, deeper verification for uncertain/high-risk actions.\n"
    "When uncertainty is high, verify preconditions before irreversible actions.\n"
    "</research_family_instructions>"
)

REWOO_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Follow a modular plan-work-solve pattern.\n"
    "First draft structured steps, then execute one step at a time with intermediate evidence, and finally synthesize results.\n"
    "</research_family_instructions>"
)

TREE_SEARCH_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Generate multiple candidate plans, score them with a risk-aware rubric, execute the top one, and backtrack once if it fails.\n"
    "</research_family_instructions>"
)

CRITIC_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Run an explicit actor-critic refinement cycle before high-impact actions.\n"
    "The critic must verify preconditions, safety constraints, and inventory/tool readiness.\n"
    "</research_family_instructions>"
)

GRAPH_MEMORY_ADDENDUM = (
    "\n\n<research_family_instructions>\n"
    "Maintain a compact graph memory of objectives, actions, outcomes, and dependencies.\n"
    "Retrieve relevant graph snippets before choosing the next action and check for contradictions.\n"
    "</research_family_instructions>"
)


WEATHER_APP_AGENT_ADDENDUM = (
    "\n\n<app_expert_instructions>\n"
    "You are the weather and field-conditions expert.\n"
    "Prioritize: current weather, short forecast, wind/rain risk, and go/no-go recommendations for field operations.\n"
    "If conditions are unsafe, explicitly block action and provide safe alternatives.\n"
    "</app_expert_instructions>"
)

SENSOR_APP_AGENT_ADDENDUM = (
    "\n\n<app_expert_instructions>\n"
    "You are the sensor interpretation expert.\n"
    "Prioritize: soil moisture/temperature, canopy indicators, anomalies, and confidence notes.\n"
    "Always report measured evidence before recommending actions.\n"
    "</app_expert_instructions>"
)

MACHINERY_APP_AGENT_ADDENDUM = (
    "\n\n<app_expert_instructions>\n"
    "You are the machinery and implement operations expert.\n"
    "Prioritize: equipment status, precondition checks, attachment compatibility, fuel/consumable readiness, and safe execution order.\n"
    "Do not execute irreversible actions unless readiness checks pass.\n"
    "</app_expert_instructions>"
)

OPERATIONS_APP_AGENT_ADDENDUM = (
    "\n\n<app_expert_instructions>\n"
    "You are the farm operations coordination expert.\n"
    "Prioritize: operational sequencing, ridge-level targeting, inventory consistency, and completion evidence.\n"
    "Return a concise completion summary with unresolved risks if any remain.\n"
    "</app_expert_instructions>"
)


class AgentConfigBuilder:
    """Build FAIRY controller configs with ARE-compatible family semantics."""

    def __init__(
        self, farm_system_prompt: str = "", building_system_prompt: str = ""
    ) -> None:
        self.farm_system_prompt = farm_system_prompt
        self.building_system_prompt = building_system_prompt

    def build(self, agent_name: str) -> ControllerAgentConfig:
        farm_prompt = self.farm_system_prompt or FARM_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
        generic_prompt = self.farm_system_prompt or DEFAULT_FUNCTION_CALL_SYSTEM_PROMPT
        building_prompt = (
            self.building_system_prompt or BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
        )
        match agent_name:
            case "default":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=generic_prompt,
                        max_iterations=80,
                    ),
                )
            case "farm_world":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt,
                        max_iterations=80,
                    ),
                )
            case "farm_baseline_react":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt,
                        max_iterations=80,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="react",
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "building_baseline_react":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=building_prompt,
                        max_iterations=100,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="react",
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_planner_executor":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + PLANNING_ADDENDUM,
                        max_iterations=100,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="planner_executor",
                        replan_on_tool_error=True,
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_reflective_memory":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + REFLECTIVE_ADDENDUM,
                        max_iterations=100,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="reflective",
                        reflection=ResearchMemoryConfig(
                            enabled=True, max_items=12, injection_top_k=4
                        ),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_multi_specialist":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + DELEGATION_ADDENDUM,
                        max_iterations=110,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="multi_specialist",
                        delegation=ResearchDelegationConfig(
                            enabled=True,
                            specialists=[
                                "weather_specialist",
                                "soil_specialist",
                                "equipment_specialist",
                                "operations_specialist",
                            ],
                            max_delegations_per_turn=4,
                        ),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_adaptive_verifier":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + VERIFIER_ADDENDUM,
                        max_iterations=110,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="adaptive_verifier",
                        replan_on_tool_error=True,
                        verification=ResearchVerificationConfig(enabled=True),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_rewoo_modular":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + REWOO_ADDENDUM,
                        max_iterations=110,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="rewoo_modular",
                        replan_on_tool_error=True,
                        rewoo=ResearchReWooConfig(
                            enabled=True,
                            max_plan_steps=8,
                            max_replans=1,
                        ),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_tree_search":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + TREE_SEARCH_ADDENDUM,
                        max_iterations=110,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="tree_search",
                        tree_search=ResearchTreeSearchConfig(
                            enabled=True,
                            branch_factor=3,
                            search_depth=2,
                            max_expansions=6,
                        ),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_critic_refiner":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + CRITIC_ADDENDUM,
                        max_iterations=110,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="critic_refiner",
                        critic=ResearchCriticConfig(
                            enabled=True,
                            max_revision_cycles=2,
                            enforce_precondition_checks=True,
                        ),
                        verification=ResearchVerificationConfig(enabled=True),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case "farm_graph_memory":
                return ControllerAgentConfig(
                    agent_name=agent_name,
                    base_agent_config=ReactBaseAgentConfig(
                        system_prompt=farm_prompt + GRAPH_MEMORY_ADDENDUM,
                        max_iterations=110,
                    ),
                    research_profile=ResearchAgentProfileConfig(
                        family_id=agent_name,
                        planning_mode="graph_memory",
                        graph_memory=ResearchGraphMemoryConfig(
                            enabled=True,
                            max_nodes=60,
                            retrieval_top_k=5,
                            contradiction_check=True,
                        ),
                        telemetry=ResearchTelemetryConfig(enabled=True),
                    ),
                )
            case _:
                raise ValueError(f"Agent {agent_name} not found")


class AppAgentConfigBuilder:
    def __init__(
        self, app_system_prompt: str = DEFAULT_APP_AGENT_SYSTEM_MESSAGE
    ) -> None:
        self.app_system_prompt = app_system_prompt

    def list_agents(self) -> list[str]:
        return [
            "default_app_agent",
            "weather_expert_app_agent",
            "sensor_expert_app_agent",
            "machinery_expert_app_agent",
            "operations_expert_app_agent",
        ]

    def build(self, agent_name: str) -> AppAgentConfig:
        base_prompt = self.app_system_prompt
        match agent_name:
            case "default_app_agent":
                return AppAgentConfig(
                    agent_name=agent_name,
                    system_prompt=base_prompt,
                    max_iterations=80,
                )
            case "weather_expert_app_agent":
                return AppAgentConfig(
                    agent_name=agent_name,
                    system_prompt=base_prompt + WEATHER_APP_AGENT_ADDENDUM,
                    max_iterations=60,
                )
            case "sensor_expert_app_agent":
                return AppAgentConfig(
                    agent_name=agent_name,
                    system_prompt=base_prompt + SENSOR_APP_AGENT_ADDENDUM,
                    max_iterations=60,
                )
            case "machinery_expert_app_agent":
                return AppAgentConfig(
                    agent_name=agent_name,
                    system_prompt=base_prompt + MACHINERY_APP_AGENT_ADDENDUM,
                    max_iterations=70,
                )
            case "operations_expert_app_agent":
                return AppAgentConfig(
                    agent_name=agent_name,
                    system_prompt=base_prompt + OPERATIONS_APP_AGENT_ADDENDUM,
                    max_iterations=70,
                )
            case _:
                raise ValueError(f"Sub agent {agent_name} not found")
