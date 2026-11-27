from smartoffice.controllers.agent_builder import AgentBuilder
from smartoffice.controllers.agent_config import (
    BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT,
    DEFAULT_FUNCTION_CALL_SYSTEM_PROMPT,
    AgentConfigBuilder,
    get_system_prompt_for_run,
)


def test_building_controller_uses_domain_prompt_and_is_discoverable() -> None:
    config = AgentConfigBuilder().build("building_baseline_react")

    assert "building_baseline_react" in AgentBuilder().list_agents()
    assert (
        config.base_agent_config.system_prompt
        == BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
    )
    assert "advance_time" in config.base_agent_config.system_prompt
    assert "command being accepted" in config.base_agent_config.system_prompt
    assert (
        "never add or subtract a timezone offset"
        in config.base_agent_config.system_prompt
    )
    assert "A booking-only request ends after the reservation" in (
        config.base_agent_config.system_prompt
    )
    assert "Never execute a future meeting's preparation early" in (
        config.base_agent_config.system_prompt
    )


def test_prompt_selection_uses_scenario_domain_without_mislabeling_default() -> None:
    assert (
        get_system_prompt_for_run(
            "default", "scenario_building_kechuang_k1315_conference_standard"
        )
        == BUILDING_WORLD_FUNCTION_CALL_SYSTEM_PROMPT
    )
    assert get_system_prompt_for_run("default", "generic_scenario") == (
        DEFAULT_FUNCTION_CALL_SYSTEM_PROMPT
    )
