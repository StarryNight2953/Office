# agents/agent/agent.py

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

CST = timezone(timedelta(hours=8))

from fairy.agents.agent.base_agent import BaseAgent
from fairy.agents.agent.toolset_builder import build_toolset
from fairy.scenarios.workflow import WorkflowStep


class AgentRunLimitExceeded(RuntimeError):
    def __init__(self, limit_type: str, limit_value: float):
        self.limit_type = limit_type
        self.limit_value = limit_value
        super().__init__(f"Agent run stopped by {limit_type}: {limit_value}")


class RecoverableToolCallError(RuntimeError):
    def __init__(self, original: Exception, tool_name: str, tool_call_id: str):
        self.original = original
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        super().__init__(f"{type(original).__name__}: {original}")


def _is_rate_limit_error(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}".lower()
    return (
        "ratelimiterror" in text
        or "rate limit" in text
        or "too many requests" in text
        or "error code: 429" in text
        or "http/1.1 429" in text
        or "status code: 429" in text
    )


def _is_non_retryable_llm_error(error: Exception) -> bool:
    """Do not retry requests that the provider says cannot fit or are invalid."""

    status_code = getattr(error, "status_code", None)
    if status_code in {400, 413, 422}:
        return True
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "maximum context length",
            "context length exceeded",
            "prompt is too long",
            "too many tokens",
        )
    )


class Agent(BaseAgent):

    terminal_tool_names = frozenset(
        {"AgentUserInterface__send_message_to_user"}
    )

    def __init__(
        self,
        name,
        llm,
        system_message="",
        messages=None,
        toolsets=None,
        hidden_tool_names: set[str] | frozenset[str] | None = None,
    ):
        """
        Agent extends BaseAgent with function-calling logic.
        
        Args:
            name (str): The agent's name.
            model_client: An instance of an LLM client.
            system_message (str, optional): A system instruction message.
            messages (optional): A Messages instance holding the conversation history.
            toolsets (list, optional): A list of Class objects (e.g., database API) from which agent tools are extracted.
        """
        super().__init__(name, llm, system_message, messages)
        self.tools = None
        self.tool_schemas = None
        self.tools_map = None
        if toolsets is not None:
            self.tools, self.tool_schemas, self.tools_map = \
                build_toolset(toolsets, excluded_tool_names=hidden_tool_names)
        self.tool_call_count = 0
        self.llm_turn_count = 0
        self.stop_reason: str | None = None
        # Domain controllers may reject a premature final response and return
        # public blockers.  The callback is optional so legacy scenarios keep
        # their historical termination semantics.
        self.completion_guard: Callable[[], Mapping[str, Any]] | None = None
        self.completion_guard_rejections = 0
        # A trusted domain controller may finish a verified subtask or hand a
        # blocked/deferred subtask back to its caller without another LLM turn.
        self.run_resolution_guard: Callable[[], Mapping[str, Any] | None] | None = None
        self.run_resolution: dict[str, Any] | None = None
        self.max_completion_guard_rejections: int | None = None
        self.progress_guard: Callable[[], Mapping[str, Any] | None] | None = None
        self.max_stalled_progress_turns = 12
        self._domain_progress_counts: dict[str, int] = {}
        self.max_llm_call_retries = int(os.getenv("FAIRY_AGENT_LLM_CALL_RETRIES", "5"))
        self.llm_retry_sleep_seconds = float(os.getenv("FAIRY_AGENT_LLM_RETRY_SLEEP_S", "0"))
        self.rate_limit_retry_sleep_seconds = float(
            os.getenv("FAIRY_AGENT_RATE_LIMIT_SLEEP_S", "30")
        )
        # These opt-in controls affect only what is sent to the model, not the
        # full messages/workflow retained for run artifacts.
        self.max_prompt_chars: int | None = None
        self.max_repeated_identical_calls: int | None = None
        self._tool_fingerprint_counts: dict[tuple[str, str, str, float], int] = {}
        self._identical_tool_call_count = 0

    def before_llm_turn(self, task: str) -> str:
        if self.progress_guard is not None:
            state = self.progress_guard()
            if state is not None:
                key = json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
                count = self._domain_progress_counts.get(key, 0) + 1
                self._domain_progress_counts[key] = count
                if count >= self.max_stalled_progress_turns:
                    self.stop_reason = "no_progress"
                    self.record_event(
                        "agent_limit_exceeded",
                        status="stopped",
                        limit_type="no_progress",
                        limit_value=count,
                        progress_state=state,
                    )
                    raise AgentRunLimitExceeded("no_progress", count)
                if count == self.max_stalled_progress_turns // 2:
                    return (
                        "No material closeout progress. Time/status queries cannot clear "
                        "these blockers; change the device/task strategy. " + key
                    )
        return ""

    def after_llm_message(self, message: Any) -> None:
        return None

    def after_tool_response(self, tool_call: Any, tool_response: Any) -> None:
        return None

    def after_agent_error(self, exc: Exception) -> None:
        return None

    def after_agent_run(self) -> None:
        return None

    def _check_limits(
        self,
        *,
        max_tool_calls: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        if max_tool_calls is not None and self.tool_call_count >= max_tool_calls:
            self.stop_reason = "max_tool_calls"
            self.record_event(
                "agent_limit_exceeded",
                status="stopped",
                limit_type="max_tool_calls",
                limit_value=max_tool_calls,
                tool_call_count=self.tool_call_count,
            )
            raise AgentRunLimitExceeded("max_tool_calls", max_tool_calls)
        if (
            timeout_seconds is not None
            and self.run_started_at_wall is not None
            and time.perf_counter() - self.run_started_at_wall >= timeout_seconds
        ):
            self.stop_reason = "timeout"
            self.record_event(
                "agent_limit_exceeded",
                status="stopped",
                limit_type="timeout_seconds",
                limit_value=timeout_seconds,
                elapsed_wall_seconds=round(time.perf_counter() - self.run_started_at_wall, 6),
                tool_call_count=self.tool_call_count,
            )
            raise AgentRunLimitExceeded("timeout_seconds", timeout_seconds)

    def call_function(self, tool_call):

        name = tool_call.function.name
        tool_call_id = tool_call.id
        raw_arguments = tool_call.function.arguments
        tool_type = tool_call.type
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            self.record_event(
                "tool_args_parse_error",
                status="error",
                tool_name=name,
                tool_call_id=tool_call_id,
                raw_arguments=raw_arguments,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self.tool_call_count += 1
            self._append_tool_error_observation(
                tool_call=tool_call,
                tool_name=name,
                tool_type=tool_type,
                tool_args={},
                raw_arguments=raw_arguments,
                exc=exc,
                elapsed_time=0.0,
                error_stage="tool_args_parse",
            )
            raise RecoverableToolCallError(exc, name, tool_call_id) from exc

        # Record exact simulation time on both sides of the tool call.  The
        # human-readable ``time`` field remains the post-call timestamp for
        # backward compatibility with existing workflow consumers.
        env_time_before = self.time_manager.time()
        env_datetime = datetime.fromtimestamp(env_time_before, tz=CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        self.log(f"[Env Time: {env_datetime}] Calling tool: {name}({args})")

        # Call corresponding function with provided arguments
        start_time = time.perf_counter()
        message_count_before_tool = len(self.messages.messages)
        self.record_event(
            "tool_call_start",
            status="running",
            tool_name=name,
            tool_call_id=tool_call_id,
            tool_type=tool_type,
            tool_args=args,
        )
        try:
            tool_response = self.tools_map[name](**args)
        except Exception as exc:
            elapsed_time = round(time.perf_counter() - start_time, 6)
            self.record_event(
                "tool_call_error",
                status="error",
                tool_name=name,
                tool_call_id=tool_call_id,
                tool_type=tool_type,
                tool_args=args,
                wall_time_seconds=elapsed_time,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self.tool_call_count += 1
            self._append_tool_error_observation(
                tool_call=tool_call,
                tool_name=name,
                tool_type=tool_type,
                tool_args=args,
                raw_arguments=raw_arguments,
                exc=exc,
                elapsed_time=elapsed_time,
                error_stage="tool_call",
                time_before=env_time_before,
            )
            raise RecoverableToolCallError(exc, name, tool_call_id) from exc
        elapsed_time = round(time.perf_counter() - start_time, 6)
        self.tool_call_count += 1

        # If the tool injected notifications into the conversation while it was
        # running, delay them until after the required tool response message.
        deferred_messages = self.messages.messages[message_count_before_tool:]
        if deferred_messages:
            del self.messages.messages[message_count_before_tool:]

        # Add time prefix to tool response
        env_time_after = self.time_manager.time()
        env_ts = datetime.fromtimestamp(env_time_after, tz=CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.messages.tool_call_response(tool_response, tool_call, elapsed_time, env_ts)
        if deferred_messages:
            self.messages.messages.extend(deferred_messages)
        self.workflow.add_node(WorkflowStep(
            op_type="TOOL",
            tool_name=name,
            tool_args=args,
            content=tool_response,
            time=env_ts,
            time_before=env_time_before,
            time_after=env_time_after,
        )
        )
        self.record_event(
            "tool_call_end",
            status="ok",
            tool_name=name,
            tool_call_id=tool_call_id,
            tool_type=tool_type,
            tool_args=args,
            wall_time_seconds=elapsed_time,
            content_type=type(tool_response).__name__,
            content_preview=str(tool_response)[:500],
        )
        self._observe_tool_progress(
            name=name,
            args=args,
            response=tool_response,
            time_before=env_time_before,
            time_after=env_time_after,
        )
        return tool_response

    def _observe_tool_progress(
        self,
        *,
        name: str,
        args: dict[str, Any],
        response: Any,
        time_before: float,
        time_after: float,
    ) -> None:
        limit = self.max_repeated_identical_calls
        if limit is None or name in self.terminal_tool_names:
            return
        operation = name.rsplit("__", 1)[-1]
        response_fingerprint = (
            "state-changing-command"
            if operation.startswith(("set_", "cancel_", "create_", "submit_"))
            else json.dumps(self._jsonable(response), sort_keys=True, ensure_ascii=False)
        )
        if isinstance(response, Mapping) and isinstance(response.get("progress_key"), str):
            response_fingerprint = response["progress_key"]
        # TimeManager.time() includes wall-clock drift. Its offset changes only
        # when simulation time is explicitly advanced, which is the progress
        # boundary relevant to repeated model actions.
        clock_offset = getattr(self.time_manager, "offset", None)
        logical_time = (
            float(clock_offset)
            if clock_offset is not None
            else float(int(time_after // 60))
        )
        fingerprint = (
            name,
            json.dumps(args, sort_keys=True, ensure_ascii=False, default=str),
            response_fingerprint,
            logical_time,
        )
        self._identical_tool_call_count = self._tool_fingerprint_counts.get(fingerprint, 0) + 1
        self._tool_fingerprint_counts[fingerprint] = self._identical_tool_call_count
        if self._identical_tool_call_count == max(2, limit // 2):
            self.messages.controller_notify(
                f"The same {name} call returned the same result "
                f"{self._identical_tool_call_count} times without an explicit "
                "time advance. Change strategy or advance time only after checking "
                "urgent commitments; do not repeat this identical call.",
                time=self.env_time_str(),
            )
        if self._identical_tool_call_count >= limit:
            self.stop_reason = "no_progress"
            self.record_event(
                "agent_limit_exceeded",
                status="stopped",
                limit_type="no_progress",
                limit_value=limit,
                tool_name=name,
                tool_call_count=self.tool_call_count,
            )
            raise AgentRunLimitExceeded("no_progress", limit)

    def _prompt_messages(self) -> list[Any]:
        """Return a bounded model view while keeping the audited history intact."""

        limit = self.max_prompt_chars
        full_history = self.messages()
        if limit is None:
            return full_history
        compatible = self._llm_compatible_messages(full_history)

        def size(message: dict[str, Any]) -> int:
            return len(json.dumps(message, ensure_ascii=False, default=str))

        if sum(size(item) for item in compatible) <= limit:
            return compatible
        first_user = next(
            (index for index, item in enumerate(compatible) if item.get("role") == "user"),
            len(compatible),
        )
        head = compatible[: first_user + 1]
        marker = {
            "role": "user",
            "content": (
                "[Controller notice] Older conversation turns were omitted from "
                "this model request to stay within the context budget. The full "
                "audit history is retained. Recheck current state with targeted "
                "tools; do not repeat broad overview queries without a reason."
            ),
        }
        available = max(0, limit - sum(size(item) for item in head) - size(marker))
        tail: list[dict[str, Any]] = []
        for original in reversed(compatible[first_user + 1 :]):
            item = dict(original)
            content = item.get("content")
            if isinstance(content, str) and len(content) > 20_000:
                item["content"] = (
                    content[:20_000]
                    + "\n[Tool/controller text truncated in prompt; full text remains in artifacts.]"
                )
            item_size = size(item)
            if item_size > available:
                break
            tail.append(item)
            available -= item_size
        tail.reverse()
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
        selected = [*head, marker, *tail]
        self.record_event(
            "prompt_context_compacted",
            status="ok",
            retained_message_count=len(selected),
            omitted_message_count=len(compatible) - len(head) - len(tail),
            prompt_chars=sum(size(item) for item in selected),
        )
        return selected

    def _append_tool_error_observation(
        self,
        *,
        tool_call: Any,
        tool_name: str,
        tool_type: str,
        tool_args: dict[str, Any],
        raw_arguments: str,
        exc: Exception,
        elapsed_time: float,
        error_stage: str,
        time_before: float | None = None,
    ) -> None:
        env_time_after = self.time_manager.time()
        env_ts = datetime.fromtimestamp(env_time_after, tz=CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        error_text = (
            f"[OUTPUT OF TOOL {tool_name}] ERROR:\n"
            "***\n"
            f"{type(exc).__name__}: {exc}\n"
            f"Raw arguments: {raw_arguments}\n"
            "***\n\n"
            "Now retry with valid arguments or choose a different tool. "
            "Do not repeat the same invalid call."
        )
        self.messages.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": error_text,
                "time": env_ts,
            }
        )
        self.messages.runtime_metrics.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "time": elapsed_time,
                "status": "error",
            }
        )
        self.workflow.add_node(
            WorkflowStep(
                op_type="TOOL_ERROR",
                tool_name=tool_name,
                tool_args=tool_args,
                content=error_text,
                time=env_ts,
                time_before=(
                    float(time_before) if time_before is not None else env_time_after
                ),
                time_after=env_time_after,
            )
        )
        self.record_event(
            "tool_error_observation",
            status="ok",
            tool_name=tool_name,
            tool_call_id=tool_call.id,
            tool_type=tool_type,
            tool_args=tool_args,
            error_stage=error_stage,
            error_type=type(exc).__name__,
            error_message=str(exc),
            content_preview=error_text[:500],
        )

    def _chat_completion_with_retries(self, messages, tools=None):
        attempt = 0
        while True:
            try:
                return self.chat_completion(messages, tools=tools)
            except Exception as exc:
                if _is_non_retryable_llm_error(exc):
                    raise
                if attempt >= self.max_llm_call_retries:
                    raise
                attempt += 1
                sleep_seconds = self._llm_retry_sleep_for_error(exc)
                self.record_event(
                    "llm_call_retry",
                    status="retrying",
                    attempt=attempt,
                    max_retries=self.max_llm_call_retries,
                    sleep_seconds=sleep_seconds,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)

    def _llm_retry_sleep_for_error(self, exc: Exception) -> float:
        if _is_rate_limit_error(exc):
            return max(0.0, self.rate_limit_retry_sleep_seconds)
        return max(0.0, self.llm_retry_sleep_seconds)

    def _try_finish(self) -> bool:
        """Return whether the run may finish, publishing blockers if not."""

        guard = self.completion_guard
        guard_result = guard() if guard is not None else None
        if isinstance(guard_result, Mapping) and not bool(
            guard_result.get("can_finish", True)
        ):
            self.completion_guard_rejections += 1
            blockers = list(guard_result.get("blockers", []))
            limit = self.max_completion_guard_rejections
            if limit is not None and self.completion_guard_rejections >= limit:
                self.stop_reason = "completion_verification"
                self.record_event("agent_limit_exceeded", status="stopped",
                    limit_type="completion_verification", limit_value=limit, blockers=blockers)
                raise AgentRunLimitExceeded("completion_verification", limit)
            required_action = guard_result.get("required_action", (
                "Call BuildingOperationsApp__get_operational_status, resolve or "
                "wait through the blockers, then attempt completion again."
            ))
            notice = (
                "Completion rejected by the state verification guard. "
                f"{required_action} "
                f"Blockers: {json.dumps(blockers, ensure_ascii=False)}"
            )
            self.messages.controller_notify(notice, time=self.env_time_str())
            self.workflow.add_node(
                WorkflowStep(
                    op_type="CONTROLLER",
                    content=notice,
                    time=self.env_time_str(),
                )
            )
            self.record_event(
                "completion_guard_rejected",
                status="continue",
                rejection_count=self.completion_guard_rejections,
                blockers=blockers,
            )
            return False
        self.stop_reason = "agent_finished"
        return True

    def _resolve_run(self) -> dict[str, Any] | None:
        if self.run_resolution_guard is None:
            return None
        resolution = self.run_resolution_guard()
        if resolution is None:
            return None
        status = resolution.get("status")
        if status not in {"ok", "blocked", "deferred"}:
            raise ValueError("invalid controller run resolution")
        # Successful early termination must satisfy the ordinary guard too.
        if (
            status == "ok"
            and self.completion_guard is not None
            and not self.completion_guard().get("can_finish", False)
        ):
            return None
        self.run_resolution = dict(resolution)
        self.stop_reason = (
            "verified_postconditions" if status == "ok" else f"task_{status}"
        )
        self.record_event(
            "controller_run_resolution", status=status, resolution=self.run_resolution
        )
        self.workflow.add_node(
            WorkflowStep(
                op_type="CONTROLLER",
                content=self.run_resolution,
                time=self.env_time_str(),
            )
        )
        return self.run_resolution

    def run(
        self,
        input,
        *,
        max_tool_calls: int | None = None,
        timeout_seconds: float | None = None,
    ):
        """
        Add the user input to the conversation history, then call chat_completion()
        """
        self.run_started_at_wall = time.perf_counter()
        self.run_wall_timeout_seconds = timeout_seconds
        self.stop_reason = None
        self.run_resolution = None
        self.completion_guard_rejections = 0
        self._tool_fingerprint_counts = {}
        self._identical_tool_call_count = 0
        self._domain_progress_counts = {}
        env_ts = self.env_time_str()
        self.record_event(
            "agent_run_start",
            status="running",
            max_tool_calls=max_tool_calls,
            timeout_seconds=timeout_seconds,
        )
        self.messages.user_input(input, time=env_ts)
        self.workflow.add_node(WorkflowStep(op_type="USER", content=input, time=env_ts))
        final_answer = None

        try:
            while True:
                resolution = self._resolve_run()
                if resolution is not None:
                    final_answer = resolution.get("answer", "")
                    break
                self._check_limits(max_tool_calls=max_tool_calls, timeout_seconds=timeout_seconds)
                turn_context = self.before_llm_turn(str(input))
                if turn_context.strip():
                    self.messages.controller_notify(turn_context, time=self.env_time_str())
                    self.record_event(
                        "controller_context",
                        status="ok",
                        content_preview=turn_context[:1000],
                    )
                message = self._chat_completion_with_retries(
                    self._prompt_messages(), tools=self.tool_schemas
                )
                self.llm_turn_count += 1
                self.after_llm_message(message)
                if not message.tool_calls:
                    if not self._try_finish():
                        continue
                    final_answer = message.content
                    if str(message.content or "").strip():
                        self.workflow.add_node(
                            WorkflowStep(
                                op_type="ASSISTANT",
                                content=message.content,
                                time=self.env_time_str(),
                            )
                        )
                    break

                # === handle tool calls ===
                terminal_tool_called = False
                for tool_call in message.tool_calls:
                    self._check_limits(max_tool_calls=max_tool_calls, timeout_seconds=timeout_seconds)
                    try:
                        tool_response = self.call_function(tool_call)
                    except RecoverableToolCallError as exc:
                        self.after_agent_error(exc.original)
                        continue
                    self.after_tool_response(tool_call, tool_response)
                    if tool_call.function.name in self.terminal_tool_names:
                        terminal_tool_called = True
                        final_answer = tool_response
                if terminal_tool_called and self._try_finish():
                    break
            wall_elapsed = round(time.perf_counter() - self.run_started_at_wall, 6)
            self.after_agent_run()
            self.record_event(
                "agent_run_end",
                status=(self.run_resolution or {}).get("status", "ok"),
                stopped_reason=self.stop_reason,
                wall_time_seconds=wall_elapsed,
                tool_call_count=self.tool_call_count,
                llm_turn_count=self.llm_turn_count,
            )
            return final_answer
        except Exception as exc:
            wall_elapsed = round(time.perf_counter() - self.run_started_at_wall, 6)
            if (
                isinstance(exc, AgentRunLimitExceeded)
                and self.stop_reason in {"max_tool_calls", "timeout", "no_progress", "completion_verification"}
            ):
                self.after_agent_run()
                self.record_event(
                    "agent_run_end",
                    status="stopped",
                    stopped_reason=self.stop_reason,
                    wall_time_seconds=wall_elapsed,
                    tool_call_count=self.tool_call_count,
                    llm_turn_count=self.llm_turn_count,
                    limit_type=exc.limit_type,
                    limit_value=exc.limit_value,
                )
                raise

            self.after_agent_error(exc)
            self.record_event(
                "agent_run_error",
                status="error",
                stopped_reason=self.stop_reason or "error",
                wall_time_seconds=wall_elapsed,
                tool_call_count=self.tool_call_count,
                llm_turn_count=self.llm_turn_count,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            raise
