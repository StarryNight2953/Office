"""Offline tests for the staged Building live-model runner."""

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pytest


def _runner_module():
    path = Path(__file__).parents[1] / "scripts" / "building_l3_live_runner.py"
    spec = importlib.util.spec_from_file_location("building_l3_live_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_runner_stages_and_cli_command_are_stable(tmp_path) -> None:
    runner = _runner_module()

    smoke = runner.scenario_ids_for_stage("smoke")
    diagnostic = runner.scenario_ids_for_stage("diagnostic")
    repair = runner.scenario_ids_for_stage("repair")
    constraint_repair = runner.scenario_ids_for_stage("constraint_repair")
    environment_repair = runner.scenario_ids_for_stage("environment_repair")
    final_repairs = runner.scenario_ids_for_stage("final_repairs")
    comfort_repair = runner.scenario_ids_for_stage("comfort_repair")
    all_scenarios = runner.scenario_ids_for_stage("all")
    selected = runner.scenario_ids_for_numbers("3, 14,3,16")
    command = runner.build_cli_command(
        smoke[0],
        endpoint="http://example.test/v1",
        model="served-model",
        output_dir=tmp_path,
        max_tool_calls=180,
        timeout_seconds=1800,
        max_output_tokens=4096,
    )
    multi_command = runner.build_cli_command(
        smoke[0],
        endpoint="http://example.test/v1",
        model="served-model",
        output_dir=tmp_path,
        max_tool_calls=180,
        timeout_seconds=1800,
        max_output_tokens=4096,
        agent_mode="multi",
        fair_building_eval=True,
    )

    assert len(smoke) == 1
    assert len(diagnostic) == 6
    assert len(repair) == 6
    assert {"_13_", "_14_"} <= {
        f"_{scenario_id.split('_l3_')[1].split('_')[0]}_"
        for scenario_id in repair
    }
    assert {
        int(scenario_id.split("_l3_")[1].split("_")[0])
        for scenario_id in constraint_repair
    } == {16, 20}
    assert len(environment_repair) == 1
    assert "_l3_20_" in environment_repair[0]
    assert {
        int(scenario_id.split("_l3_")[1].split("_")[0])
        for scenario_id in final_repairs
    } == {5, 13, 20}
    assert len(comfort_repair) == 1
    assert "_l3_13_" in comfort_repair[0]
    assert len(all_scenarios) == 20
    assert len(set(all_scenarios)) == 20
    assert len(selected) == 3
    assert "_l3_03_" in selected[0]
    assert "_l3_14_" in selected[1]
    with pytest.raises(ValueError, match="unknown"):
        runner.scenario_ids_for_numbers("3,21")
    assert "building_baseline_react" in command
    assert "vllm" in command
    assert "false" in command
    assert "http://example.test/v1" in command
    mode_index = command.index("--building-agent-mode")
    assert command[mode_index + 1] == "single"
    multi_mode_index = multi_command.index("--building-agent-mode")
    assert multi_command[multi_mode_index + 1] == "multi"
    assert multi_command[multi_command.index("--room-max-tool-calls") + 1] == "80"
    assert multi_command[multi_command.index("--max-prompt-chars") + 1] == "100000"
    assert multi_command[multi_command.index("--max-repeated-identical-calls") + 1] == "6"
    assert "--fair-building-eval" not in command
    assert "--fair-building-eval" in multi_command


def _fair_args(tmp_path) -> Namespace:
    return Namespace(
        output_root=tmp_path,
        building_agent_mode="single",
        model="served-model",
        endpoint="http://example.test/v1",
        max_tool_calls=500,
        timeout_seconds=3600.0,
        max_output_tokens=4096,
        room_max_tool_calls=80,
        room_timeout_seconds=300.0,
        max_prompt_chars=100_000,
        max_repeated_identical_calls=6,
        fair_building_eval=True,
        dry_run=False,
        rerun=False,
    )


def test_fair_pair_alternates_order_and_writes_matching_manifests(tmp_path, monkeypatch) -> None:
    runner = _runner_module()
    args = _fair_args(tmp_path)
    monkeypatch.setattr(runner, "source_fingerprint", lambda: "fixed-code")
    observed = []

    def fake_cell(cell_args, scenario_id, index, total):
        observed.append((index, cell_args.building_agent_mode, scenario_id))
        return {
            "scenario_id": scenario_id,
            "validation_success": True,
            "status": "completed",
            "report_path": str(cell_args.output_root / scenario_id / "report.json"),
        }

    monkeypatch.setattr(runner, "_run_cell", fake_cell)
    runner.run_fair_pair(args)

    assert [mode for _, mode, _ in observed[:4]] == [
        "single", "multi", "multi", "single"
    ]
    assert len(observed) == 40
    single = runner.json.loads((tmp_path / "single" / "experiment_manifest.json").read_text())
    multi = runner.json.loads((tmp_path / "multi" / "experiment_manifest.json").read_text())
    assert single["source_sha256"] == multi["source_sha256"] == "fixed-code"
    assert single["protocol"] == multi["protocol"] == "building_l3_fair_ab_v5"
    assert single["event_guarded_live_time"] is True
    assert single["automatic_bounded_wait"] is True
    assert single["structured_room_postconditions"] is True
    assert single["room_usage_accounting"] == "lifetime_across_resets"
    assert single["verified_contract_early_finish"] is True
    assert single["adaptive_plateau_monitoring"] is True
    assert single["operational_horizon_clamp"] is True
    assert single["closeout_no_progress_turn_limit"] == 12
    assert {k: v for k, v in single.items() if k != "mode"} == {
        k: v for k, v in multi.items() if k != "mode"
    }
    assert len(runner.json.loads((tmp_path / "single" / "summary.json").read_text())) == 20
    assert len(runner.json.loads((tmp_path / "multi" / "summary.json").read_text())) == 20

    changed_args = _fair_args(tmp_path / "single")
    changed_args.max_tool_calls = 501
    with pytest.raises(RuntimeError, match="different code or settings"):
        runner.write_fair_manifest(changed_args, runner.scenario_ids_for_stage("all"))


def test_fair_manifest_and_reuse_reject_old_reports(tmp_path, monkeypatch) -> None:
    runner = _runner_module()
    monkeypatch.setattr(runner, "source_fingerprint", lambda: "fixed-code")
    args = _fair_args(tmp_path)
    scenario_id = runner.scenario_ids_for_stage("all")[0]
    report_path = tmp_path / scenario_id / f"{scenario_id}.run_report.json"
    report_path.parent.mkdir()
    report_path.write_text("{}")
    with pytest.raises(RuntimeError, match="no fair manifest"):
        runner.write_fair_manifest(args, (scenario_id,))

    report_path.unlink()
    runner.write_fair_manifest(args, (scenario_id,))
    report_path.write_text(
        runner.json.dumps(
            {
                "scenario_id": scenario_id,
                "model": args.model,
                "limits": {"fair_building_eval": False},
            }
        )
    )
    with pytest.raises(RuntimeError, match="not from this fair-run cell"):
        runner._run_cell(args, scenario_id, 1, 1)

    report_path.write_text(
        runner.json.dumps(
            {
                "scenario_id": scenario_id,
                "model": args.model,
                "provider": "vllm",
                "controller_id": "building_baseline_react",
                "limits": {
                    "fair_building_eval": True,
                    "fair_building_protocol": "building_l3_fair_ab_v5",
                    "feasible_device_contracts": True,
                    "guarded_device_plans": True,
                    "same_room_goal_coalescing": True,
                    "approved_plan_wait": True,
                    "verified_wait_event_continuation": True,
                    "background_room_mutation": False,
                    "room_usage_accounting": "lifetime_across_resets",
                    "adaptive_plateau_monitoring": True,
                    "automatic_bounded_wait": True,
                    "operational_horizon_clamp": True,
                    "closeout_no_progress_turn_limit": 12,
                    "simulation_clock": "explicit",
                    "max_tool_calls": args.max_tool_calls,
                    "timeout_seconds": args.timeout_seconds,
                    "max_output_tokens": args.max_output_tokens,
                    "parallel_tool_calls": False,
                },
                "controller_telemetry": {},
                "evaluation": {"validation": {"success": True}},
            }
        )
    )
    assert runner._run_cell(args, scenario_id, 1, 1)["validation_success"] is True
    args.building_agent_mode = "multi"
    with pytest.raises(RuntimeError, match="not from this fair-run cell"):
        runner._run_cell(args, scenario_id, 1, 1)


def test_failure_row_is_reportable(tmp_path) -> None:
    runner = _runner_module()
    report_path = tmp_path / "missing.run_report.json"

    row = runner._failure_row(
        "scenario_building_test",
        report_path,
        status="wrapper_timeout",
        error="timed out",
    )
    runner._write_summary(tmp_path, [row])

    assert row["validation_success"] is False
    assert row["return_code"] is None
    assert "wrapper_timeout" in (tmp_path / "summary.md").read_text()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.environ is case-insensitive on Windows; NO_PROXY/no_proxy share one entry",
)
def test_private_endpoint_is_added_to_no_proxy(monkeypatch) -> None:
    runner = _runner_module()
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.delenv("no_proxy", raising=False)

    runner.configure_direct_endpoint("http://100.115.106.71:8000/v1")

    assert runner.os.environ["NO_PROXY"] == "localhost,100.115.106.71"
    assert runner.os.environ["no_proxy"] == "100.115.106.71"
