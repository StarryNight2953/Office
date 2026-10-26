"""Regression contracts for long-running Agent model requests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fairy.agents.agent.agent import Agent, AgentRunLimitExceeded
from fairy.time_manager import TimeManager


def _agent() -> Agent:
    agent = Agent(
        "safeguard-test",
        SimpleNamespace(provider="openai", model="fake", max_output_tokens=4096),
        system_message="Keep the original task.",
        toolsets=[],
    )
    clock = TimeManager()
    clock.reset(start_time=0.0)
    agent.set_time_manager(clock)
    return agent


def test_closeout_progress_guard_ignores_clock_and_resets_for_real_progress() -> None:
    agent = _agent()
    state = {"blockers": [{"code": "devices_still_on", "device_ids": ["hvac", "fan"]}]}
    agent.progress_guard = lambda: state
    agent.max_stalled_progress_turns = 4
    for offset in (0, 3600, 7200):
        agent.time_manager.set_offset(offset)
        agent.before_llm_turn("closeout")
    # Actually switching one device off gives a new progress state.
    state["blockers"][0]["device_ids"] = ["fan"]
    for _ in range(3):
        agent.before_llm_turn("closeout")
    with pytest.raises(AgentRunLimitExceeded, match="no_progress"):
        agent.before_llm_turn("closeout")


def test_no_progress_guard_is_inactive_outside_domain_closeout() -> None:
    agent = _agent()
    agent.progress_guard = lambda: None
    for _ in range(30):
        assert agent.before_llm_turn("normal operation") == ""


def test_verified_tool_progress_key_ignores_run_and_reset_counters() -> None:
    agent = _agent()
    agent.max_repeated_identical_calls = 3
    for index in range(2):
        agent._observe_tool_progress(name="RoomAgentManagerApp__delegate_to_room", args={"room_id": "x"},
            response={"status": "blocked", "progress_key": "same-canonical-blocker", "run_id": index},
            time_before=0, time_after=0)
    with pytest.raises(AgentRunLimitExceeded, match="no_progress"):
        agent._observe_tool_progress(name="RoomAgentManagerApp__delegate_to_room", args={"room_id": "x"},
            response={"status": "blocked", "progress_key": "same-canonical-blocker", "run_id": 50},
            time_before=0, time_after=0)


def test_prompt_window_keeps_full_audit_history_and_tool_pairs() -> None:
    agent = _agent()
    agent.max_prompt_chars = 5_000
    agent.messages.user_input("Operate through the day")
    for index in range(20):
        call_id = f"call-{index}"
        agent.messages.messages.append(
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "Status__read", "arguments": "{}"},
                    }
                ],
            }
        )
        agent.messages.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "observation " + ("x" * 750),
            }
        )
    full_count = len(agent.messages.messages)

    prompt = agent._prompt_messages()

    assert len(agent.messages.messages) == full_count
    assert sum(len(json.dumps(item, ensure_ascii=False)) for item in prompt) <= 5_000
    assert prompt[0]["role"] == "system"
    assert prompt[1]["content"] == "Operate through the day"
    assert "Older conversation turns were omitted" in prompt[2]["content"]
    assert prompt[3]["role"] == "assistant"
    assert prompt[-1]["tool_call_id"] == "call-19"
    assert any(
        event["event_type"] == "prompt_context_compacted"
        for event in agent.runtime_events
    )


def test_exact_repeated_tool_results_stop_without_time_advancement() -> None:
    agent = _agent()
    agent.max_repeated_identical_calls = 6

    for instant in range(2):
        agent._observe_tool_progress(
            name="BuildingWorldApp__get_building_overview",
            args={},
            response={"rooms": []},
            time_before=float(instant),
            time_after=float(instant) + 0.1,
        )
    agent._observe_tool_progress(
        name="Other__read",
        args={},
        response={"status": "ok"},
        time_before=0.0,
        time_after=0.0,
    )
    for _ in range(3):
        agent._observe_tool_progress(
            name="BuildingWorldApp__get_building_overview",
            args={},
            response={"rooms": []},
            time_before=0.0,
            time_after=0.0,
        )
    assert any("do not repeat" in item.get("content", "") for item in agent.messages.messages)

    with pytest.raises(AgentRunLimitExceeded) as stopped:
        agent._observe_tool_progress(
            name="BuildingWorldApp__get_building_overview",
            args={},
            response={"rooms": []},
            time_before=0.0,
            time_after=0.0,
        )
    assert stopped.value.limit_type == "no_progress"
    assert agent.stop_reason == "no_progress"

    agent.run_started_at_wall = None
    agent._tool_fingerprint_counts = {}
    agent._identical_tool_call_count = 0
    for instant in range(10):
        agent.time_manager.set_offset(float(instant))
        agent._observe_tool_progress(
            name="SystemApp__advance_time",
            args={"minutes": 5},
            response={"status": "ok"},
            time_before=float(instant),
            time_after=float(instant + 1),
        )
    assert agent._identical_tool_call_count == 1


def test_non_retryable_context_error_is_not_retried() -> None:
    class ContextError(RuntimeError):
        status_code = 400

    agent = _agent()
    attempts = 0

    def fail(_messages, tools=None):
        nonlocal attempts
        attempts += 1
        raise ContextError("maximum context length exceeded")

    agent.chat_completion = fail  # type: ignore[method-assign]

    with pytest.raises(ContextError):
        agent._chat_completion_with_retries([], tools=[])
    assert attempts == 1
    assert not any(
        event["event_type"] == "llm_call_retry" for event in agent.runtime_events
    )
