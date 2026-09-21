"""Offline tests for the staged Building L1 live-model runner."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from fairy.scenarios.building_kechuang.l1.manifest import (
    accepted_contracts,
    load_manifest,
)


def _runner_module():
    path = Path(__file__).parents[1] / "scripts" / "building_l1_live_runner.py"
    spec = importlib.util.spec_from_file_location("building_l1_live_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BuildingL1LiveRunnerTests(unittest.TestCase):
    def test_l1_live_runner_stages_and_cli_command_are_stable(self) -> None:
        runner = _runner_module()
        smoke = runner.scenario_ids_for_stage("smoke")
        repair = runner.scenario_ids_for_stage("repair")
        meeting = runner.scenario_ids_for_stage("meeting")
        diagnostic = runner.scenario_ids_for_stage("diagnostic")
        all_scenarios = runner.scenario_ids_for_stage("all")
        expected = tuple(
            contract["scenario_id"]
            for contract in accepted_contracts(load_manifest())
        )

        with tempfile.TemporaryDirectory() as directory:
            command = runner.shared.build_cli_command(
                smoke[0],
                endpoint="http://example.test/v1",
                model="served-model",
                output_dir=Path(directory),
                max_tool_calls=80,
                timeout_seconds=900,
                max_output_tokens=4096,
            )

        self.assertEqual(len(smoke), 1)
        self.assertTrue(smoke[0].endswith("_hvac_control"))
        self.assertEqual(
            repair,
            (
                "scenario_building_kechuang_l1_ventilation_control",
                "scenario_building_kechuang_l1_power_constrained_adjustment",
            ),
        )
        self.assertEqual(
            meeting,
            (
                "scenario_building_kechuang_l1_meeting_room_booking",
                "scenario_building_kechuang_l1_meeting_requirement_change",
            ),
        )
        self.assertEqual(len(diagnostic), 6)
        self.assertEqual(len(set(diagnostic)), 6)
        self.assertLessEqual(set(diagnostic), set(all_scenarios))
        self.assertEqual(all_scenarios, expected)
        self.assertEqual(len(all_scenarios), 12)
        self.assertEqual(len(set(all_scenarios)), 12)
        self.assertIn("building_baseline_react", command)
        self.assertIn("vllm", command)
        self.assertIn("false", command)
        self.assertIn("http://example.test/v1", command)

    def test_l1_summary_uses_l1_title(self) -> None:
        runner = _runner_module()
        with tempfile.TemporaryDirectory() as directory:
            output_root = Path(directory)
            report_path = output_root / "missing.run_report.json"
            row = runner.shared._failure_row(
                "scenario_building_kechuang_l1_test",
                report_path,
                status="wrapper_timeout",
                error="timed out",
            )

            runner.shared._write_summary(
                output_root,
                [row],
                experiment_title="Building L1 live-model experiment",
            )

            summary = (output_root / "summary.md").read_text(encoding="utf-8")
            self.assertTrue(
                summary.startswith("# Building L1 live-model experiment\n")
            )
            self.assertIn("wrapper_timeout", summary)
