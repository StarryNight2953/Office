from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))


def json_default(value: Any) -> str:
    return str(value)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=json_default)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with open(temporary_path, "w", encoding="utf-8") as f:
        f.writelines(
            json.dumps(row, ensure_ascii=False, default=json_default) + "\n"
            for row in rows
        )
    temporary_path.replace(path)


def token_usage_from_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "source": "llm_runtime_metrics",
    }
    for entry in metrics:
        tokens = entry.get("tokens") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens"):
            totals[key] += int(tokens.get(key) or 0)
    if totals["total_tokens"] == 0:
        totals["source"] = "not_applicable_without_llm"
    return totals


def metric_totals(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    assistant = [entry for entry in metrics if entry.get("role") == "assistant"]
    tools = [entry for entry in metrics if entry.get("role") == "tool"]
    return {
        "llm_call_count": len(assistant),
        "tool_metric_count": len(tools),
        "llm_wall_time_seconds": round(sum(float(entry.get("time") or 0) for entry in assistant), 6),
        "tool_wall_time_seconds": round(sum(float(entry.get("time") or 0) for entry in tools), 6),
        "token_usage": token_usage_from_metrics(metrics),
    }


def merge_metric_totals(
    primary: dict[str, Any], additional: dict[str, Any] | None
) -> dict[str, Any]:
    if not additional:
        return primary
    merged = {
        "llm_call_count": int(primary.get("llm_call_count") or 0)
        + int(additional.get("llm_call_count") or 0),
        "tool_metric_count": int(primary.get("tool_metric_count") or 0)
        + int(additional.get("tool_metric_count") or 0),
        "llm_wall_time_seconds": round(
            float(primary.get("llm_wall_time_seconds") or 0)
            + float(additional.get("llm_wall_time_seconds") or 0),
            6,
        ),
        "tool_wall_time_seconds": round(
            float(primary.get("tool_wall_time_seconds") or 0)
            + float(additional.get("tool_wall_time_seconds") or 0),
            6,
        ),
    }
    primary_tokens = primary.get("token_usage") or {}
    additional_tokens = additional.get("token_usage") or {}
    token_usage = {
        key: int(primary_tokens.get(key) or 0)
        + int(additional_tokens.get(key) or 0)
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cached_tokens",
        )
    }
    token_usage["source"] = (
        "llm_runtime_metrics"
        if token_usage["total_tokens"]
        else "not_applicable_without_llm"
    )
    merged["token_usage"] = token_usage
    return merged


def artifact_guide(artifacts: dict[str, str]) -> dict[str, str]:
    labels = {
        "run_report": "Open this first: status, metrics, outcome, errors, and file guide.",
        "runtime_log": "Chronological runtime log: prompts, LLM calls, tokens, tool calls, limits, and errors.",
        "agent_workflow": "Semantic replay/audit trace: ordered USER/TOOL steps with tool args and results.",
        "oracle_workflow": "Oracle workflow built from the scenario.",
        "replay_workflow": "Workflow produced by replaying an oracle or saved workflow.",
        "messages": "Raw LLM conversation and tool responses for prompt/debug analysis.",
        "runtime_metrics": "Per LLM/tool timing and token details.",
        "a2a_traces": "A2A expert-agent messages, runtime events, workflow, and token details.",
        "multi_agent_traces": "Supervisor dispatch, room-Agent traces, and routed Building events.",
        "multi_agent_state": "Restorable room inbox, lifecycle, counters, traces, and router state.",
        "run_log": "Tool-only replay log kept for compatibility.",
        "evaluation": "Scenario validation result and farm outcome summary.",
        "run_summary": "Legacy machine summary kept for compatibility with older scripts.",
    }
    return {key: labels.get(key, "Generated run artifact.") for key in artifacts}


def build_run_report(
    *,
    scenario_id: str,
    controller_id: str | None,
    provider: str | None,
    model: str | None,
    temperature: float | None,
    run_type: str,
    stopped_reason: str,
    wall_time_seconds: float,
    max_tool_calls: int | None,
    timeout_seconds: float | None,
    parallel_tool_calls: bool | None,
    max_output_tokens: int | None,
    workflow_steps: int,
    tool_call_count: int,
    runtime_metrics: list[dict[str, Any]],
    artifacts: dict[str, str],
    system_prompt_mode: str | None = None,
    evaluation: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    controller_telemetry: dict[str, Any] | None = None,
    a2a: dict[str, Any] | None = None,
    additional_metric_totals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    totals = merge_metric_totals(
        metric_totals(runtime_metrics), additional_metric_totals
    )
    status = "completed" if stopped_reason in {"agent_finished", "completed"} and error is None else "stopped"
    if error is not None:
        status = "failed"
    if stopped_reason in {"timeout", "timeout_seconds", "max_tool_calls", "no_progress"}:
        status = "stopped"
    return {
        "schema_version": 1,
        "run_type": run_type,
        "scenario_id": scenario_id,
        "controller_id": controller_id,
        "provider": provider,
        "model": model,
        "temperature": temperature,
        "system_prompt_mode": system_prompt_mode,
        "status": status,
        "stopped_reason": stopped_reason,
        "stop_reason": stopped_reason,
        "wall_time_seconds": round(wall_time_seconds, 6),
        "limits": {
            "max_tool_calls": max_tool_calls,
            "timeout_seconds": timeout_seconds,
            "parallel_tool_calls": parallel_tool_calls,
            "max_output_tokens": max_output_tokens,
        },
        "metrics": {
            "wall_time_seconds": round(wall_time_seconds, 6),
            "workflow_steps": workflow_steps,
            "tool_call_count": tool_call_count,
            **totals,
            "controller_telemetry": controller_telemetry or {},
        },
        "controller_telemetry": controller_telemetry or {},
        "a2a": a2a or {"enabled": False},
        "evaluation": evaluation,
        "outcome": outcome or {},
        "error": error,
        "artifacts": artifacts,
        "artifact_guide": artifact_guide(artifacts),
        "created_at": datetime.now(tz=CST).isoformat(),
    }
