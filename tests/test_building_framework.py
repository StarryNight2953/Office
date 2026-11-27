"""Building runtime, model-client and tool-loop regression contracts."""

from __future__ import annotations

import importlib
import json
import pkgutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import smartoffice
from smartoffice.agents.agent.agent import AgentRunLimitExceeded
from smartoffice.agents.agent.base_agent import CST
from smartoffice.agents.llm.base_llm import BaseLLM
from smartoffice.apps.agent_user_interface import AgentUserInterface
from smartoffice.apps.system import SystemApp
from smartoffice.controllers.agent_builder import AppAgentBuilder, build_controller_agent
from smartoffice.controllers.agent_config import AppAgentConfigBuilder
from smartoffice.controllers.app_agent import AppAgent, apply_a2a_to_apps, collect_a2a_traces
from smartoffice.controllers.engine import Engine
from smartoffice.scenarios.registry import get_scenario_class
from smartoffice.scenarios.scenario import Scenario
from smartoffice.scenarios.workflow import Workflow, WorkflowStep


class _ToolScenario(Scenario):
    """Minimal fixture for the tool loop used by building controllers."""

    scenario_input = "Check the current time."

    def initiate_scenario(self):
        self.apps = [AgentUserInterface(), SystemApp()]


def test_all_building_runtime_modules_import() -> None:
    failures = []
    for info in pkgutil.walk_packages(smartoffice.__path__, smartoffice.__name__ + "."):
        try:
            importlib.import_module(info.name)
        except Exception as exc:  # pragma: no cover - reported in assertion
            failures.append((info.name, type(exc).__name__, str(exc)))
    assert failures == []


def test_no_are_or_rsare_runtime_imports() -> None:
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "rg",
            r"from are\.|import are\.|from rsare\.|import rsare\.",
            "smartoffice",
            "tests",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1, result.stdout + result.stderr


def test_workflow_save_creates_parent_directory(tmp_path: Path) -> None:
    workflow = Workflow()
    workflow.add_node(
        WorkflowStep(name="check_time", tool_name="SystemApp__get_current_time")
    )
    path = tmp_path / "new" / "nested" / "workflow.json"

    workflow.save_workflow(str(path))

    assert Workflow.load_workflow(str(path)).to_dict() == workflow.to_dict()


class _DummyLLM:
    provider = "openai"

    def chat_completion(self, messages, tools=None):  # pragma: no cover - not called
        raise AssertionError("controller construction should not call the LLM")


class _PromptDetails:
    cached_tokens = 3


class _Usage:
    prompt_tokens = 11
    completion_tokens = 5
    total_tokens = 16
    prompt_tokens_details = _PromptDetails()


class _FunctionCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    type = "function"

    def __init__(self, name: str, arguments: str = "{}") -> None:
        self.id = f"call_{name}"
        self.function = _FunctionCall(name, arguments)


class _Message:
    def __init__(self, content: str | None = None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Response:
    usage = _Usage()

    def __init__(self, message: _Message) -> None:
        self.choices = [_Choice(message)]


class _ScriptedLLM:
    provider = "openai"
    model = "fake-model"

    def __init__(self, messages: list[_Message]) -> None:
        self.messages = list(messages)

    def chat_completion(self, messages, tools=None):
        if not self.messages:
            return _Response(_Message(content="done", tool_calls=None))
        return _Response(self.messages.pop(0))


class _FailingLLM:
    provider = "openai"
    model = "failing-model"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def chat_completion(self, messages, tools=None):
        raise self.exc


class _FlakyLLM:
    provider = "openai"
    model = "flaky-model"

    def __init__(self, exc: Exception, message: _Message) -> None:
        self.exc = exc
        self.message = message
        self.calls = 0

    def chat_completion(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            raise self.exc
        return _Response(self.message)


def test_a2a_expert_agent_limit_can_be_capped_by_runner_budget() -> None:
    scenario = get_scenario_class("scenario_building_kechuang_room_booking")()
    scenario.setup()
    result = apply_a2a_to_apps(
        list(scenario.apps or []),
        llm_factory=lambda: _ScriptedLLM([_Message(content="done")]),
        app_agent_builder=AppAgentBuilder(),
        app_agent_config_builder=AppAgentConfigBuilder(),
        app_prop=1.0,
        policy="generic",
        app_agent_name="default_app_agent",
        seed=scenario.seed,
        max_tool_calls=3,
    )

    wrappers = [app for app in result.apps if isinstance(app, AppAgent)]
    assert wrappers
    assert all(app.max_tool_calls == 3 for app in wrappers)


def test_a2a_expert_agent_can_call_wrapped_app_tool() -> None:
    weather = SystemApp()
    config = AppAgentConfigBuilder().build("default_app_agent")
    expert = AppAgentBuilder().build(
        config,
        llm=_ScriptedLLM(
            [
                _Message(tool_calls=[_ToolCall("SystemApp__get_current_time")]),
                _Message(content="time checked", tool_calls=None),
            ]
        ),
        app=weather,
    )
    wrapper = AppAgent(
        wrapped_app=weather,
        app_agent=expert,
        app_agent_name="default_app_agent",
    )

    answer = wrapper.expert_agent("Check the current time.")

    assert answer == "time checked"
    assert wrapper.last_tool_call_count == 1
    user_messages = [
        message
        for message in wrapper.app_agent.messages.messages
        if isinstance(message, dict) and message["role"] == "user"
    ]
    assert user_messages
    expected_time = datetime.fromtimestamp(
        wrapper.time_manager.time(), tz=CST
    ).strftime("%Y-%m-%d %H:%M:%S")
    assert f"Received at: {expected_time}" in user_messages[0]["content"]
    assert "Sender: User" in user_messages[0]["content"]
    assert "Message: Check the current time." in user_messages[0]["content"]
    assert "Already read: True" in user_messages[0]["content"]
    assert f"Read at: {expected_time}" in user_messages[0]["content"]
    assert any(
        event["event_type"] == "tool_call_end" for event in wrapper.last_runtime_events
    )


def test_a2a_expert_agent_limit_returns_partial_result() -> None:
    weather = SystemApp()
    config = AppAgentConfigBuilder().build("default_app_agent")
    expert = AppAgentBuilder().build(
        config,
        llm=_ScriptedLLM(
            [_Message(tool_calls=[_ToolCall("SystemApp__get_current_time")])]
        ),
        app=weather,
    )
    wrapper = AppAgent(
        wrapped_app=weather,
        app_agent=expert,
        app_agent_name="default_app_agent",
        max_tool_calls=0,
    )

    answer = wrapper.expert_agent("Check the current time.")

    assert "Expert agent stopped before completing the delegated task" in answer
    assert "max_tool_calls=0" in answer
    assert wrapper.last_answer == answer
    assert wrapper.last_tool_call_count == 0
    assert any(
        event["event_type"] == "agent_limit_exceeded" and event["status"] == "stopped"
        for event in wrapper.last_runtime_events
    )
    assert any(
        event["event_type"] == "agent_run_end" and event["status"] == "stopped"
        for event in wrapper.last_runtime_events
    )
    assert any(
        event["event_type"] == "a2a_expert_limit_returned"
        and event["status"] == "stopped"
        for event in wrapper.last_runtime_events
    )


def test_collect_a2a_traces_exports_expert_prompt_events_and_metrics() -> None:
    weather = SystemApp()
    config = AppAgentConfigBuilder().build("default_app_agent")
    expert = AppAgentBuilder().build(
        config,
        llm=_ScriptedLLM(
            [
                _Message(tool_calls=[_ToolCall("SystemApp__get_current_time")]),
                _Message(content="time checked", tool_calls=None),
            ]
        ),
        app=weather,
    )
    wrapper = AppAgent(
        wrapped_app=weather,
        app_agent=expert,
        app_agent_name="default_app_agent",
    )

    wrapper.expert_agent("Check the current time.")
    traces = collect_a2a_traces([wrapper])

    assert traces[0]["wrapper_app_name"] == "SystemApp"
    assert traces[0]["app_agent"] == "default_app_agent"
    assert "smart-building simulation" in traces[0]["messages"][0]["content"]
    assert "Message: Check the current time." in traces[0]["messages"][1]["content"]
    assert traces[0]["tool_call_count"] == 1
    assert traces[0]["runtime_metrics"][0]["tokens"]["total_tokens"] == 16
    assert any(
        event["event_type"] == "tool_call_end" for event in traces[0]["runtime_events"]
    )
    assert "step1" in traces[0]["workflow"]


def test_qwen_and_vllm_clients_use_function_call_path(monkeypatch) -> None:
    monkeypatch.setenv("QWEN_API_KEY", "test-key")
    monkeypatch.setenv("VLLM_API_KEY", "EMPTY")

    qwen = BaseLLM.llm_builder(
        {"provider": "qwen", "model": "qwen3.6-flash-2026-04-16"}
    )
    qwen_legacy = BaseLLM.llm_builder(
        {"provider": "qwen-json", "model": "qwen3.6-flash-2026-04-16"}
    )
    vllm = BaseLLM.llm_builder(
        {
            "provider": "vllm",
            "model": "Qwen/Qwen3-1.7B",
            "base_url": "http://localhost:8000/v1",
        }
    )

    assert qwen.provider == "qwen"
    assert qwen_legacy.provider == "qwen"
    assert vllm.provider == "vllm"


def test_vllm_qwen_client_disables_thinking_in_chat_template(monkeypatch) -> None:
    monkeypatch.setenv("VLLM_API_KEY", "EMPTY")
    monkeypatch.delenv("FAIRY_VLLM_ENABLE_THINKING", raising=False)
    captured: dict = {}

    class _CaptureCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Response(_Message(content="ok", tool_calls=None))

    class _CaptureChat:
        completions = _CaptureCompletions()

    class _CaptureClient:
        chat = _CaptureChat()

    vllm = BaseLLM.llm_builder(
        {
            "provider": "vllm",
            "model": "Qwen3.5-9B",
            "base_url": "http://localhost:8000/v1",
            "parallel_tool_calls": False,
        }
    )
    vllm.api_client = _CaptureClient()

    vllm.chat_completion([{"role": "user", "content": "Return ok"}], tools=[])

    assert captured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert captured["parallel_tool_calls"] is False
    assert captured["max_tokens"] == 2048


def test_deepseek_client_passes_parallel_tool_calls(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: dict = {}

    class _CaptureCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Response(_Message(content="ok", tool_calls=None))

    class _CaptureChat:
        completions = _CaptureCompletions()

    class _CaptureClient:
        chat = _CaptureChat()

    deepseek = BaseLLM.llm_builder(
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "parallel_tool_calls": False,
            "max_output_tokens": 4096,
        }
    )
    deepseek.api_client = _CaptureClient()

    deepseek.chat_completion([{"role": "user", "content": "Return ok"}], tools=[])

    assert captured["parallel_tool_calls"] is False
    assert captured["max_tokens"] == 4096


def test_llm_max_output_tokens_default_and_environment_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.delenv("FAIRY_LLM_MAX_OUTPUT_TOKENS", raising=False)

    default_client = BaseLLM.llm_builder(
        {"provider": "deepseek", "model": "deepseek-chat"}
    )
    assert default_client.max_output_tokens == 2048

    monkeypatch.setenv("FAIRY_LLM_MAX_OUTPUT_TOKENS", "3072")
    environment_client = BaseLLM.llm_builder(
        {"provider": "deepseek", "model": "deepseek-chat"}
    )
    assert environment_client.max_output_tokens == 3072

    with pytest.raises(ValueError, match="positive integer"):
        BaseLLM.llm_builder(
            {
                "provider": "deepseek",
                "model": "deepseek-chat",
                "max_output_tokens": 0,
            }
        )


def test_openai_compatible_clients_accept_request_timeout(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("FAIRY_LLM_REQUEST_TIMEOUT_S", "12.5")

    deepseek = BaseLLM.llm_builder({"provider": "deepseek", "model": "deepseek-chat"})
    qwen = BaseLLM.llm_builder({"provider": "qwen", "model": "qwen-plus", "timeout": 7})

    assert deepseek.request_timeout == 12.5
    assert qwen.request_timeout == 7.0


def test_agent_runtime_events_capture_llm_tool_metrics() -> None:
    scenario = _ToolScenario()
    scenario.setup()
    llm = _ScriptedLLM(
        [
            _Message(tool_calls=[_ToolCall("SystemApp__get_current_time")]),
            _Message(content="done", tool_calls=None),
        ]
    )
    agent = build_controller_agent(
        "building_baseline_react",
        llm=llm,
        toolsets=scenario.apps,
    )
    engine = Engine(agent, scenario)

    workflow = engine.run_scenario_agent(
        max_tool_calls=5,
        timeout_seconds=30,
        setup_scenario=False,
    )

    event_types = [event["event_type"] for event in agent.runtime_events]
    assert "llm_call_start" in event_types
    assert "llm_call_end" in event_types
    assert "tool_call_start" in event_types
    assert "tool_call_end" in event_types
    assert "agent_run_end" in event_types
    assert agent.tool_call_count == 1
    assert agent.telemetry["tool_calls"] == 1
    assert agent.telemetry["llm_calls"] == 2
    assert len(workflow) == 3  # USER, TOOL, and final ASSISTANT response.
    tool_step = next(step for step in workflow.dag.values() if step.op_type == "TOOL")
    assert isinstance(tool_step.time_before, float)
    assert isinstance(tool_step.time_after, float)
    assert tool_step.time_after >= tool_step.time_before
    assert agent.messages.runtime_metrics[0]["tokens"]["total_tokens"] == 16
    llm_starts = [
        event
        for event in agent.runtime_events
        if event["event_type"] == "llm_call_start"
    ]
    llm_start = llm_starts[0]
    assert llm_start["max_output_tokens"] is None
    assert llm_start["messages"][0]["role"] == "system"
    assert llm_start["messages"][-1]["role"] == "user"
    assert "SystemApp__get_current_time" in llm_start["tool_names"]
    assert all(
        "time" not in message for event in llm_starts for message in event["messages"]
    )
    llm_ends = [
        event for event in agent.runtime_events if event["event_type"] == "llm_call_end"
    ]
    tool_call_end = llm_ends[0]
    final_end = llm_ends[1]
    assert tool_call_end["assistant_content"] is None
    assert tool_call_end["assistant_content_is_empty"] is True
    assert tool_call_end["rationale"] is None
    assert tool_call_end["rationale_source"] == "empty_assistant_content"
    assert (
        tool_call_end["tool_calls"][0]["function"]["name"]
        == "SystemApp__get_current_time"
    )
    assert final_end["assistant_content"] == "done"
    assert final_end["assistant_content_is_empty"] is False
    assert final_end["rationale"] == "done"
    assert final_end["rationale_source"] == "assistant_content"


def test_agent_runtime_event_sink_receives_live_events() -> None:
    scenario = _ToolScenario()
    scenario.setup()
    agent = build_controller_agent(
        "building_baseline_react",
        llm=_ScriptedLLM([_Message(content="done", tool_calls=None)]),
        toolsets=scenario.apps,
    )
    live_events = []
    agent.runtime_event_sink = live_events.append

    Engine(agent, scenario).run_scenario_agent(
        max_tool_calls=2,
        timeout_seconds=30,
        setup_scenario=False,
    )

    assert [event["event_type"] for event in live_events] == [
        event["event_type"] for event in agent.runtime_events
    ]
    assert live_events[-1]["event_type"] == "agent_run_end"


def test_live_progress_omits_full_message_history(tmp_path: Path) -> None:
    from smartoffice.cli import _append_live_progress

    progress_path = tmp_path / "scenario.progress.jsonl"
    _append_live_progress(
        progress_path,
        {
            "event_index": 3,
            "event_type": "llm_call_start",
            "messages": [{"role": "user", "content": "large context"}],
            "message_count": 1,
        },
    )

    row = json.loads(progress_path.read_text(encoding="utf-8"))
    assert row["record_index"] == 3
    assert row["record_type"] == "llm_call_start"
    assert row["message_count"] == 1
    assert "messages" not in row


def test_engine_appends_scenario_additional_system_prompt_for_agent_run() -> None:
    scenario = _ToolScenario()
    scenario.additional_system_prompt = "SCENARIO-SPECIFIC-SYSTEM-HINT"
    scenario.setup()
    agent = build_controller_agent(
        "building_baseline_react",
        llm=_ScriptedLLM([_Message(content="done", tool_calls=None)]),
        toolsets=scenario.apps,
    )

    Engine(agent, scenario).run_scenario_agent(
        max_tool_calls=2,
        timeout_seconds=30,
        setup_scenario=False,
    )

    llm_start = next(
        event
        for event in agent.runtime_events
        if event["event_type"] == "llm_call_start"
    )
    assert "SCENARIO-SPECIFIC-SYSTEM-HINT" in llm_start["messages"][0]["content"]


def test_agent_runtime_events_capture_limits_and_parse_errors() -> None:
    scenario = _ToolScenario()
    scenario.setup()
    limited_agent = build_controller_agent(
        "building_baseline_react",
        llm=_ScriptedLLM(
            [_Message(tool_calls=[_ToolCall("SystemApp__get_current_time")])]
        ),
        toolsets=scenario.apps,
    )
    limited_engine = Engine(limited_agent, scenario)

    with pytest.raises(AgentRunLimitExceeded):
        limited_engine.run_scenario_agent(
            max_tool_calls=0,
            timeout_seconds=30,
            setup_scenario=False,
        )
    assert any(
        event["event_type"] == "agent_limit_exceeded"
        for event in limited_agent.runtime_events
    )
    assert any(
        event["event_type"] == "agent_run_end" and event["status"] == "stopped"
        for event in limited_agent.runtime_events
    )
    assert not any(
        event["event_type"] == "agent_run_error"
        for event in limited_agent.runtime_events
    )

    parse_scenario = _ToolScenario()
    parse_scenario.setup()
    parse_agent = build_controller_agent(
        "building_baseline_react",
        llm=_ScriptedLLM(
            [
                _Message(
                    tool_calls=[_ToolCall("SystemApp__get_current_time", "{bad json")]
                )
            ]
        ),
        toolsets=parse_scenario.apps,
    )
    parse_engine = Engine(parse_agent, parse_scenario)

    parse_engine.run_scenario_agent(
        max_tool_calls=5,
        timeout_seconds=30,
        setup_scenario=False,
    )
    assert any(
        event["event_type"] == "tool_args_parse_error"
        for event in parse_agent.runtime_events
    )
    assert any(
        event["event_type"] == "tool_error_observation"
        and event["error_stage"] == "tool_args_parse"
        for event in parse_agent.runtime_events
    )
    assert any(
        message.get("role") == "tool"
        and "JSONDecodeError" in str(message.get("content"))
        and "Now retry with valid arguments" in str(message.get("content"))
        for message in parse_agent.messages.messages
        if isinstance(message, dict)
    )
    assert any(
        event["event_type"] == "agent_run_end" and event["status"] == "ok"
        for event in parse_agent.runtime_events
    )
    assert not any(
        event["event_type"] == "agent_run_error" for event in parse_agent.runtime_events
    )


def test_agent_runtime_events_capture_llm_call_errors() -> None:
    scenario = _ToolScenario()
    scenario.setup()
    agent = build_controller_agent(
        "building_baseline_react",
        llm=_FailingLLM(RuntimeError("provider rejected number schema")),
        toolsets=scenario.apps,
    )
    engine = Engine(agent, scenario)

    with pytest.raises(RuntimeError, match="provider rejected number schema"):
        engine.run_scenario_agent(
            max_tool_calls=5,
            timeout_seconds=30,
            setup_scenario=False,
        )

    llm_errors = [
        event
        for event in agent.runtime_events
        if event["event_type"] == "llm_call_error"
    ]
    assert llm_errors
    assert llm_errors[0]["status"] == "error"
    assert llm_errors[0]["error_type"] == "RuntimeError"
    assert "provider rejected number schema" in llm_errors[0]["error_message"]
    assert any(
        event["event_type"] == "agent_run_error"
        and event["error_type"] == "RuntimeError"
        for event in agent.runtime_events
    )


def test_agent_runtime_events_capture_llm_call_retry_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAIRY_AGENT_RATE_LIMIT_SLEEP_S", "0")
    scenario = _ToolScenario()
    scenario.setup()
    llm = _FlakyLLM(
        RuntimeError("temporary rate limit"),
        _Message(content="done", tool_calls=None),
    )
    agent = build_controller_agent(
        "building_baseline_react",
        llm=llm,
        toolsets=scenario.apps,
    )
    engine = Engine(agent, scenario)

    engine.run_scenario_agent(
        max_tool_calls=5,
        timeout_seconds=30,
        setup_scenario=False,
    )

    assert llm.calls == 2
    assert any(
        event["event_type"] == "llm_call_error"
        and "temporary rate limit" in event["error_message"]
        for event in agent.runtime_events
    )
    assert any(
        event["event_type"] == "llm_call_retry"
        and event["status"] == "retrying"
        and event["sleep_seconds"] == 0
        for event in agent.runtime_events
    )
    assert any(
        event["event_type"] == "agent_run_end" and event["status"] == "ok"
        for event in agent.runtime_events
    )
    assert not any(
        event["event_type"] == "agent_run_error" for event in agent.runtime_events
    )


def test_agent_llm_retry_defaults_wait_for_rate_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FAIRY_AGENT_LLM_CALL_RETRIES", raising=False)
    monkeypatch.delenv("FAIRY_AGENT_LLM_RETRY_SLEEP_S", raising=False)
    monkeypatch.delenv("FAIRY_AGENT_RATE_LIMIT_SLEEP_S", raising=False)

    agent = build_controller_agent(
        "building_baseline_react",
        llm=_ScriptedLLM([_Message(content="done", tool_calls=None)]),
        toolsets=[],
    )

    assert agent.max_llm_call_retries == 5
    assert agent.llm_retry_sleep_seconds == 0
    assert agent.rate_limit_retry_sleep_seconds == 30
    assert agent._llm_retry_sleep_for_error(RuntimeError("error code: 429")) == 30
    assert agent._llm_retry_sleep_for_error(RuntimeError("connection reset")) == 0


def test_agent_runtime_events_capture_tool_call_errors() -> None:
    scenario = _ToolScenario()
    scenario.setup()
    agent = build_controller_agent(
        "building_baseline_react",
        llm=_ScriptedLLM(
            [
                _Message(
                    tool_calls=[
                        _ToolCall(
                            "SystemApp__get_current_time",
                            '{"bad_arg": 1}',
                        )
                    ]
                )
            ]
        ),
        toolsets=scenario.apps,
    )
    engine = Engine(agent, scenario)

    engine.run_scenario_agent(
        max_tool_calls=5,
        timeout_seconds=30,
        setup_scenario=False,
    )

    tool_errors = [
        event
        for event in agent.runtime_events
        if event["event_type"] == "tool_call_error"
    ]
    assert tool_errors
    assert tool_errors[0]["status"] == "error"
    assert tool_errors[0]["tool_name"] == "SystemApp__get_current_time"
    assert tool_errors[0]["tool_args"] == {
        "bad_arg": 1,
    }
    assert tool_errors[0]["error_type"] == "TypeError"
    assert tool_errors[0]["error_message"]
    assert any(
        event["event_type"] == "tool_error_observation"
        and event["error_stage"] == "tool_call"
        for event in agent.runtime_events
    )
    assert any(
        message.get("role") == "tool"
        and "TypeError" in str(message.get("content"))
        and "Now retry with valid arguments" in str(message.get("content"))
        for message in agent.messages.messages
        if isinstance(message, dict)
    )
    assert any(
        event["event_type"] == "agent_run_end" and event["status"] == "ok"
        for event in agent.runtime_events
    )
    assert not any(
        event["event_type"] == "agent_run_error" for event in agent.runtime_events
    )


def test_run_report_exposes_human_stop_reason_and_wall_time() -> None:
    from smartoffice.controllers.run_artifacts import build_run_report

    report = build_run_report(
        scenario_id="scenario_demo",
        controller_id="building_baseline_react",
        provider="deepseek",
        model="deepseek-chat",
        temperature=0.1,
        system_prompt_mode="building",
        run_type="agent",
        stopped_reason="max_tool_calls",
        wall_time_seconds=12.3456789,
        max_tool_calls=3,
        timeout_seconds=30,
        parallel_tool_calls=False,
        max_output_tokens=2048,
        workflow_steps=4,
        tool_call_count=3,
        runtime_metrics=[],
        artifacts={},
        additional_metric_totals={
            "llm_call_count": 2,
            "tool_metric_count": 3,
            "llm_wall_time_seconds": 1.25,
            "tool_wall_time_seconds": 0.5,
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "cached_tokens": 2,
            },
        },
    )

    assert report["status"] == "stopped"
    assert report["stopped_reason"] == "max_tool_calls"
    assert report["stop_reason"] == "max_tool_calls"
    assert report["wall_time_seconds"] == 12.345679
    assert report["system_prompt_mode"] == "building"
    assert report["limits"]["parallel_tool_calls"] is False
    assert report["limits"]["max_output_tokens"] == 2048
    assert report["metrics"]["llm_call_count"] == 2
    assert report["metrics"]["tool_metric_count"] == 3
    assert report["metrics"]["token_usage"]["total_tokens"] == 14


def test_registry_and_controller_list_are_building_only():
    from smartoffice.controllers.agent_builder import AgentBuilder
    from smartoffice.scenarios.registry import list_scenarios

    scenarios = list_scenarios()
    assert len(scenarios) == 37
    assert all(name.startswith("scenario_building_kechuang_") for name in scenarios)
    assert AgentBuilder().list_agents() == ["building_baseline_react"]


def test_building_controller_preserves_hidden_briefing_tool_and_usage():
    scenario = get_scenario_class("scenario_building_kechuang_room_booking")()
    scenario.setup()
    agent = build_controller_agent(
        "building_baseline_react", _DummyLLM(), scenario.apps
    )
    assert "AgentUserInterface__send_message_to_agent" not in agent.tools_map
    assert "AgentUserInterface__send_message_to_user" in agent.tools_map
    assert "DeviceRegistryApp__get_device_state" in agent.tools_map
