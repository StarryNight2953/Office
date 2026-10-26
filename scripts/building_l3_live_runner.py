"""Run staged Kechuang Building L3 experiments against a live vLLM endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fairy.scenarios.building_kechuang.l3.catalog import L3_SPECS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENDPOINT = os.getenv(
    "FAIRY_VLLM_ENDPOINT", "http://100.115.106.71:8000/v1"
)
DEFAULT_MODEL = os.getenv("FAIRY_VLLM_MODEL", "Qwen3.6-35B-A3B-FP8")

# Start small, then add scenarios that isolate the main failure modes before
# spending time on the complete catalog.
STAGES = {
    "smoke": (6,),
    "diagnostic": (6, 11, 12, 16, 18, 20),
    # Regression anchors for termination, readiness, interaction-time print
    # disclosure and continuous occupied-environment control.
    "repair": (6, 12, 13, 14, 16, 20),
    "constraint_repair": (16, 20),
    "environment_repair": (20,),
    # Previously failing cells affected by early air-quality intervention and
    # timestamp-accurate asynchronous print completion.
    "final_repairs": (5, 13, 20),
    "comfort_repair": (13,),
    "all": tuple(range(1, 21)),
}


def configure_direct_endpoint(endpoint: str) -> None:
    """Exclude a private model host from HTTP proxies for this process tree."""

    host = urlparse(endpoint).hostname
    if not host:
        raise ValueError(f"endpoint does not contain a hostname: {endpoint!r}")
    for variable in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in os.getenv(variable, "").split(",")]
        entries = [item for item in entries if item]
        if host not in entries:
            entries.append(host)
        os.environ[variable] = ",".join(entries)


def scenario_ids_for_stage(stage: str) -> tuple[str, ...]:
    """Return stable registry IDs for one experiment stage."""

    by_number = {spec.number: spec.scenario_id for spec in L3_SPECS}
    return tuple(by_number[number] for number in STAGES[stage])


def scenario_ids_for_numbers(raw_numbers: str) -> tuple[str, ...]:
    """Select a small ordered subset without changing the standard stage lists."""

    by_number = {spec.number: spec.scenario_id for spec in L3_SPECS}
    parts = [part.strip() for part in raw_numbers.split(",")]
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError("--scenario-numbers requires comma-separated L3 numbers")
    numbers = [int(part) for part in parts]
    if any(number not in by_number for number in numbers):
        raise ValueError("--scenario-numbers contains an unknown L3 number")
    return tuple(by_number[number] for number in dict.fromkeys(numbers))


def preflight(endpoint: str, model: str, timeout_seconds: float) -> dict[str, Any]:
    """Verify model discovery and one required function call."""

    from openai import OpenAI

    client = OpenAI(api_key="EMPTY", base_url=endpoint, timeout=timeout_seconds)
    available_models = sorted(item.id for item in client.models.list().data)
    if model not in available_models:
        raise RuntimeError(
            f"requested model {model!r} is not served; available={available_models}"
        )
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        max_tokens=128,
        parallel_tool_calls=False,
        messages=[
            {
                "role": "system",
                "content": "Call ping_tool exactly once with value 1.",
            },
            {"role": "user", "content": "Perform the function-call preflight."},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "ping_tool",
                    "description": "A diagnostic function-call target.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "integer"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
            }
        ],
        tool_choice="required",
    )
    message = response.choices[0].message
    tool_calls = list(message.tool_calls or [])
    if len(tool_calls) != 1 or tool_calls[0].function.name != "ping_tool":
        raise RuntimeError("endpoint completed chat but did not return ping_tool")
    arguments = json.loads(tool_calls[0].function.arguments)
    if arguments != {"value": 1}:
        raise RuntimeError(f"unexpected ping_tool arguments: {arguments!r}")
    return {
        "endpoint": endpoint,
        "model": model,
        "available_models": available_models,
        "function_call": {"name": "ping_tool", "arguments": arguments},
    }


def build_cli_command(
    scenario_id: str,
    *,
    endpoint: str,
    model: str,
    output_dir: Path,
    max_tool_calls: int,
    timeout_seconds: float,
    max_output_tokens: int,
    agent_mode: str = "single",
    room_max_tool_calls: int = 80,
    room_timeout_seconds: float = 300.0,
    max_prompt_chars: int = 100_000,
    max_repeated_identical_calls: int = 6,
    fair_building_eval: bool = False,
) -> list[str]:
    """Construct one isolated FAIRY agent invocation."""

    command = [
        sys.executable,
        "-m",
        "fairy.cli",
        "--agent",
        "--scenario",
        scenario_id,
        "--controller",
        "building_baseline_react",
        "--building-agent-mode",
        agent_mode,
        "--provider",
        "vllm",
        "--model",
        model,
        "--endpoint",
        endpoint,
        "--temperature",
        "0.1",
        "--parallel-tool-calls",
        "false",
        "--max-tool-calls",
        str(max_tool_calls),
        "--room-max-tool-calls",
        str(room_max_tool_calls),
        "--room-timeout-seconds",
        str(room_timeout_seconds),
        "--max-prompt-chars",
        str(max_prompt_chars),
        "--max-repeated-identical-calls",
        str(max_repeated_identical_calls),
        "--timeout-seconds",
        str(timeout_seconds),
        "--max-output-tokens",
        str(max_output_tokens),
        "--output-dir",
        str(output_dir),
    ]
    if fair_building_eval:
        command.append("--fair-building-eval")
    return command


def source_fingerprint() -> str:
    """Hash the Python implementation, including uncommitted and untracked files."""

    digest = hashlib.sha256()
    paths = sorted((REPO_ROOT / "fairy").rglob("*.py"))
    paths.append(Path(__file__).resolve())
    for path in paths:
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def write_fair_manifest(args: argparse.Namespace, scenario_ids: tuple[str, ...]) -> None:
    """Refuse to mix different code or run settings in one fair batch."""

    manifest_path = args.output_root / "experiment_manifest.json"
    manifest = {
        "protocol": "building_l3_fair_ab_v5",
        "source_sha256": source_fingerprint(),
        "scenario_ids": list(scenario_ids),
        "mode": args.building_agent_mode,
        "model": args.model,
        "endpoint": args.endpoint,
        "max_tool_calls": args.max_tool_calls,
        "timeout_seconds": args.timeout_seconds,
        "max_output_tokens": args.max_output_tokens,
        "room_max_tool_calls": args.room_max_tool_calls,
        "room_timeout_seconds": args.room_timeout_seconds,
        "max_prompt_chars": args.max_prompt_chars,
        "max_repeated_identical_calls": args.max_repeated_identical_calls,
        "simulation_clock": "explicit",
        "room_device_preflight": "symmetric",
        "event_guarded_live_time": True,
        "bounded_environment_monitor": True,
        "independent_room_task_batch": True,
        "structured_room_postconditions": True,
        "room_usage_accounting": "lifetime_across_resets",
        "verified_contract_early_finish": True,
        "blocked_deferred_room_outcomes": True,
        "reconciled_empty_event_filter": True,
        "adaptive_plateau_monitoring": True,
        "feasible_device_contracts": True,
        "guarded_device_plans": True,
        "same_room_goal_coalescing": True,
        "approved_plan_wait": True,
        "verified_wait_event_continuation": True,
        "background_room_mutation": False,
        "canonical_room_context_and_verification": True,
        "automatic_bounded_wait": True,
        "operational_horizon_clamp": True,
        "closeout_no_progress_turn_limit": 12,
        "parallel_tool_calls": False,
    }
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous != manifest:
            raise RuntimeError(
                f"existing fair batch has different code or settings: {manifest_path}"
            )
        return
    if any(args.output_root.rglob("*.run_report.json")):
        raise RuntimeError(
            f"existing run reports have no fair manifest; use a new output root: {args.output_root}"
        )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def run_stage(args: argparse.Namespace) -> Path:
    """Run a stage sequentially and refresh its aggregate report per cell."""

    return run_scenarios(
        args,
        (
            scenario_ids_for_numbers(args.scenario_numbers)
            if args.scenario_numbers is not None
            else scenario_ids_for_stage(args.stage)
        ),
        experiment_title="Building L3 live-model experiment",
    )


def run_fair_pair(args: argparse.Namespace) -> Path:
    """Run all 20 matched scenes, alternating which mode runs first."""

    scenario_ids = scenario_ids_for_stage("all")
    roots = {
        mode: args.output_root / mode for mode in ("single", "multi")
    }
    rows: dict[str, list[dict[str, Any]]] = {"single": [], "multi": []}
    for mode, root in roots.items():
        root.mkdir(parents=True, exist_ok=True)
        if not args.dry_run:
            write_fair_manifest(
                argparse.Namespace(**{**vars(args), "output_root": root, "building_agent_mode": mode}),
                scenario_ids,
            )

    for index, scenario_id in enumerate(scenario_ids, start=1):
        order = ("single", "multi") if index % 2 else ("multi", "single")
        for mode in order:
            mode_args = argparse.Namespace(
                **{**vars(args), "output_root": roots[mode], "building_agent_mode": mode}
            )
            row = _run_cell(mode_args, scenario_id, index, len(scenario_ids))
            if row is not None:
                rows[mode].append(row)
                _write_summary(
                    roots[mode], rows[mode], experiment_title="Building L3 fair A/B"
                )
    if not args.dry_run:
        for mode, root in roots.items():
            _write_summary(
                root, rows[mode], experiment_title="Building L3 fair A/B"
            )
    return args.output_root


def run_scenarios(
    args: argparse.Namespace,
    scenario_ids: tuple[str, ...],
    *,
    experiment_title: str,
) -> Path:
    """Run an explicit Building scenario batch with the shared live protocol."""

    output_root = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    if getattr(args, "fair_building_eval", False) and not args.dry_run:
        write_fair_manifest(args, scenario_ids)
    rows: list[dict[str, Any]] = []

    for index, scenario_id in enumerate(scenario_ids, start=1):
        row = _run_cell(args, scenario_id, index, len(scenario_ids))
        if row is not None:
            rows.append(row)
            _write_summary(output_root, rows, experiment_title=experiment_title)

    if not args.dry_run:
        _write_summary(output_root, rows, experiment_title=experiment_title)
    return output_root / "summary.md"


def _run_cell(
    args: argparse.Namespace, scenario_id: str, index: int, total: int
) -> dict[str, Any] | None:
    if getattr(args, "fair_building_eval", False) and not args.dry_run:
        manifest_path = args.output_root / "experiment_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["source_sha256"] != source_fingerprint():
            raise RuntimeError(
                f"source changed during fair batch; stop and use a new output root: {manifest_path}"
            )
    cell_dir = args.output_root / scenario_id
    report_path = cell_dir / f"{scenario_id}.run_report.json"
    if report_path.exists() and not args.rerun:
        if getattr(args, "fair_building_eval", False):
            _verify_fair_report(report_path, args, scenario_id)
        print(f"[{index}/{total}] {args.building_agent_mode} reuse {scenario_id}", flush=True)
        return _report_row(report_path, return_code=0, reused=True)

    command = build_cli_command(
        scenario_id,
        endpoint=args.endpoint,
        model=args.model,
        output_dir=cell_dir,
        max_tool_calls=args.max_tool_calls,
        timeout_seconds=args.timeout_seconds,
        max_output_tokens=args.max_output_tokens,
        agent_mode=args.building_agent_mode,
        room_max_tool_calls=args.room_max_tool_calls,
        room_timeout_seconds=args.room_timeout_seconds,
        max_prompt_chars=args.max_prompt_chars,
        max_repeated_identical_calls=args.max_repeated_identical_calls,
        fair_building_eval=getattr(args, "fair_building_eval", False),
    )
    if args.dry_run:
        print(" ".join(command))
        return None

    cell_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = cell_dir / "stdout.log"
    stderr_path = cell_dir / "stderr.log"
    print(f"[{index}/{total}] {args.building_agent_mode} start {scenario_id}", flush=True)
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(REPO_ROOT), existing_pythonpath) if part
    )
    environment.setdefault("VLLM_API_KEY", "EMPTY")
    environment.setdefault("FAIRY_VLLM_ENABLE_THINKING", "false")
    environment.setdefault("FAIRY_LLM_REQUEST_TIMEOUT_S", "180")
    try:
        with (
            stdout_path.open("w", encoding="utf-8") as stdout_handle,
            stderr_path.open("w", encoding="utf-8") as stderr_handle,
        ):
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                env=environment,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                timeout=args.timeout_seconds + 120,
            )
    except subprocess.TimeoutExpired:
        with stderr_path.open("a", encoding="utf-8") as stderr_handle:
            stderr_handle.write(
                "\nBuilding live runner killed the process after the outer "
                f"timeout ({args.timeout_seconds + 120:.0f}s).\n"
            )
        row = _failure_row(
            scenario_id,
            report_path,
            status="wrapper_timeout",
            error="The scenario process exceeded the outer timeout.",
        )
    else:
        if report_path.exists():
            if getattr(args, "fair_building_eval", False):
                _verify_fair_report(report_path, args, scenario_id)
            row = _report_row(report_path, completed.returncode, reused=False)
        else:
            row = _failure_row(
                scenario_id,
                report_path,
                status="missing_report",
                error=f"FAIRY exited with code {completed.returncode}.",
                return_code=completed.returncode,
            )
    print(
        f"[{index}/{total}] {args.building_agent_mode} done {scenario_id}: "
        f"status={row.get('status')} validation={row.get('validation_success')}",
        flush=True,
    )
    return row


def _verify_fair_report(
    report_path: Path, args: argparse.Namespace, scenario_id: str
) -> None:
    """Avoid silently treating an old or wrong-mode cell as fair-run evidence."""

    report = json.loads(report_path.read_text(encoding="utf-8"))
    limits = report.get("limits") or {}
    telemetry = report.get("controller_telemetry") or {}
    is_multi = telemetry.get("mode") == "multi"
    expected_multi = args.building_agent_mode == "multi"
    if (
        report.get("scenario_id") != scenario_id
        or report.get("model") != args.model
        or report.get("provider") != "vllm"
        or report.get("controller_id") != "building_baseline_react"
        or limits.get("fair_building_eval") is not True
        or limits.get("fair_building_protocol") != "building_l3_fair_ab_v5"
        or limits.get("room_usage_accounting") != "lifetime_across_resets"
        or limits.get("adaptive_plateau_monitoring") is not True
        or limits.get("feasible_device_contracts") is not True
        or limits.get("guarded_device_plans") is not True
        or limits.get("same_room_goal_coalescing") is not True
        or limits.get("approved_plan_wait") is not True
        or limits.get("verified_wait_event_continuation") is not True
        or limits.get("background_room_mutation") is not False
        or limits.get("automatic_bounded_wait") is not True
        or limits.get("operational_horizon_clamp") is not True
        or limits.get("closeout_no_progress_turn_limit") != 12
        or limits.get("simulation_clock") != "explicit"
        or limits.get("max_tool_calls") != args.max_tool_calls
        or limits.get("timeout_seconds") != args.timeout_seconds
        or limits.get("max_output_tokens") != args.max_output_tokens
        or limits.get("parallel_tool_calls") is not False
        or is_multi != expected_multi
    ):
        raise RuntimeError(f"report is not from this fair-run cell: {report_path}")


def _report_row(
    report_path: Path, return_code: int, *, reused: bool
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validation = (report.get("evaluation") or {}).get("validation") or {}
    metadata = validation.get("metadata") or {}
    metrics = report.get("metrics") or {}
    tokens = metrics.get("token_usage") or {}
    return {
        "scenario_id": report.get("scenario_id"),
        "status": report.get("status"),
        "stop_reason": report.get("stop_reason"),
        "validation_success": bool(validation.get("success")),
        "return_code": return_code,
        "reused": reused,
        "tool_calls": metrics.get("tool_call_count"),
        "llm_calls": metrics.get("llm_call_count"),
        "device_plan_action_count": metrics.get("device_plan_action_count"),
        "reconciled_wait_event_count": metrics.get("reconciled_wait_event_count"),
        "total_tokens": tokens.get("total_tokens"),
        "wall_time_seconds": report.get("wall_time_seconds"),
        "comfort_violation_ratio": metadata.get(
            "occupied_comfort_violation_ratio"
        ),
        "peak_power_w": metadata.get("peak_power_w"),
        "unoccupied_energy_kwh": metadata.get("unoccupied_energy_kwh"),
        "error": report.get("error"),
        "report_path": str(report_path),
    }


def _failure_row(
    scenario_id: str,
    report_path: Path,
    *,
    status: str,
    error: str,
    return_code: int | None = None,
) -> dict[str, Any]:
    """Create a summary row when FAIRY did not produce a run report."""

    return {
        "scenario_id": scenario_id,
        "return_code": return_code,
        "status": status,
        "stop_reason": None,
        "validation_success": False,
        "tool_calls": None,
        "llm_calls": None,
        "total_tokens": None,
        "wall_time_seconds": None,
        "error": error,
        "report_path": str(report_path),
    }


def _write_summary(
    output_root: Path,
    rows: list[dict[str, Any]],
    *,
    experiment_title: str = "Building L3 live-model experiment",
) -> None:
    (output_root / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"# {experiment_title}",
        "",
        "| Scenario | Status | Stop | Validation | Tools | LLM | Tokens | Wall s | Report |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        report_path = Path(str(row["report_path"]))
        try:
            relative_report = report_path.relative_to(output_root)
        except ValueError:
            relative_report = report_path
        lines.append(
            f"| `{row.get('scenario_id')}` | {row.get('status')} | "
            f"{row.get('stop_reason')} | {row.get('validation_success')} | "
            f"{row.get('tool_calls')} | {row.get('llm_calls')} | "
            f"{row.get('total_tokens')} | {row.get('wall_time_seconds')} | "
            f"[JSON]({relative_report}) |"
        )
    (output_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _default_output_root() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return Path("runs") / f"building_qwen_l3_{timestamp}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=tuple(STAGES), default="smoke")
    parser.add_argument(
        "--scenario-numbers",
        default=None,
        help="Comma-separated L3 numbers (for example 3,14,16); overrides --stage",
    )
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--max-tool-calls", type=int, default=180)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--room-max-tool-calls", type=int, default=80)
    parser.add_argument("--room-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--max-prompt-chars", type=int, default=100_000)
    parser.add_argument("--max-repeated-identical-calls", type=int, default=6)
    parser.add_argument(
        "--fair-building-eval",
        action="store_true",
        help=(
            "Use explicit simulation time, symmetric device preflight, and "
            "event-guarded live time advance"
        ),
    )
    parser.add_argument(
        "--paired",
        action="store_true",
        help="Run all 20 single/multi scene pairs in alternating order",
    )
    parser.add_argument(
        "--building-agent-mode",
        choices=("single", "multi"),
        default="single",
        help="Choose the legacy single Agent or supervisor-plus-room Agents",
    )
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rerun", action="store_true")
    args = parser.parse_args()
    if args.scenario_numbers is not None:
        try:
            scenario_ids_for_numbers(args.scenario_numbers)
        except ValueError as exc:
            parser.error(str(exc))
    if args.paired and (args.stage != "all" or args.scenario_numbers is not None):
        parser.error("--paired requires --stage all without --scenario-numbers")
    if args.paired and not args.fair_building_eval:
        parser.error("--paired requires --fair-building-eval")
    args.output_root = args.output_root or _default_output_root()
    configure_direct_endpoint(args.endpoint)

    if args.dry_run:
        selection = (
            f"scenario_numbers={args.scenario_numbers}"
            if args.scenario_numbers is not None
            else f"stage={args.stage}"
        )
        print(f"{selection} output_root={args.output_root}")
        if args.paired:
            run_fair_pair(args)
        else:
            run_stage(args)
        return
    if not args.skip_preflight:
        result = preflight(args.endpoint, args.model, timeout_seconds=30.0)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if args.preflight_only:
        return
    summary_path = run_fair_pair(args) if args.paired else run_stage(args)
    print(summary_path)


if __name__ == "__main__":
    main()
