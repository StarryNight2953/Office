"""Formal manifest, generator and behavior tests for native Building L1s."""

from __future__ import annotations

import copy
import importlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fairy.controllers.engine import Engine
from fairy.scenarios.workflow import WorkflowStep
from fairy.scenarios.building_kechuang.l1.manifest import (
    ManifestError,
    WRITE_TOOLS,
    accepted_contracts,
    load_manifest,
    validate_manifest,
    validate_source_links,
)
from fairy.scenarios.registry import get_scenario_class
from scripts.generate_building_l1_scenarios import (
    DEFAULT_OUTPUT_DIR,
    apply_generation,
    plan_generation,
    resolve_selectors,
)
from scripts.finalize_building_l1_review import build_full_review


def _scenario_class(contract):
    importlib.import_module(
        "fairy.scenarios.building_kechuang.l1.generated."
        + contract["scenario_id"]
    )
    return get_scenario_class(contract["scenario_id"])


class BuildingL1GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_manifest()
        cls.contracts = accepted_contracts(cls.manifest)

    def test_reviewed_manifest_is_complete_and_source_linked(self):
        validate_source_links(self.manifest)
        self.assertEqual(len(self.contracts), 12)
        self.assertTrue(self.manifest["scope"]["fully_split"])
        self.assertTrue(self.manifest["scope"]["candidate_disposition_complete"])
        self.assertEqual(
            self.manifest["scope"]["source_l3_reviewed"], list(range(1, 21))
        )
        self.assertEqual(
            {item["behavior_family"] for item in self.contracts},
            {
                "air_purifier_control",
                "audio_system_control",
                "humidifier_control",
                "hvac_control",
                "lighting_control",
                "meeting_requirement_change",
                "meeting_room_booking",
                "meeting_room_recovery",
                "power_constrained_adjustment",
                "printer_control",
                "projector_control",
                "ventilation_control",
            },
        )
        claims = [
            (variant["l3_scenario_id"], action_id)
            for item in self.contracts
            for variant in item["coverage"]["source_variants"]
            for action_id in variant["action_ids"]
        ]
        self.assertEqual(len(claims), len(set(claims)))
        self.assertEqual(len(claims), 716)

    def test_manifest_rejects_unreviewed_entry(self):
        invalid = copy.deepcopy(self.manifest)
        invalid["contracts"][0]["review_status"] = "needs_review"
        with self.assertRaisesRegex(ManifestError, "must be accepted_l1"):
            validate_manifest(invalid)

    def test_selector_and_generator_are_deterministic_and_non_overwriting(self):
        selected = resolve_selectors(self.manifest, ["L3-06"])
        selected_contracts = accepted_contracts(self.manifest, selected)
        self.assertEqual(
            {item["behavior_family"] for item in selected_contracts},
            {
                "air_purifier_control",
                "hvac_control",
                "meeting_room_recovery",
                "ventilation_control",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            expected = plan_generation(selected_contracts, output_dir)
            first = apply_generation(expected, check=False, force=False)
            self.assertEqual(len(first["created"]), len(selected_contracts))
            second = apply_generation(expected, check=True, force=False)
            self.assertEqual(len(second["unchanged"]), len(selected_contracts))
            path = next(iter(expected))
            path.write_text("user change\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "not current"):
                apply_generation(expected, check=True, force=False)
            with self.assertRaisesRegex(RuntimeError, "without --force"):
                apply_generation(expected, check=False, force=False)

    def test_committed_generated_modules_match_manifest(self):
        expected = plan_generation(self.contracts, DEFAULT_OUTPUT_DIR)
        result = apply_generation(expected, check=True, force=False)
        self.assertEqual(len(result["unchanged"]), 12)

    def test_review_and_obligation_ledgers_have_complete_dispositions(self):
        root = Path(__file__).resolve().parents[1]
        review = json.loads(
            (root / "workflow_exports/building_l3_to_l1_formal/review_ledger.json")
            .read_text(encoding="utf-8")
        )
        obligations = json.loads(
            (root / "workflow_exports/building_l3_to_l1_formal/obligation_ledger.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(review["summary"]["source_l3_count"], 20)
        self.assertEqual(review["summary"]["candidate_total"], 252)
        self.assertTrue(review["summary"]["candidate_disposition_complete"])
        self.assertEqual(review["summary"]["candidate_action_total"], 711)
        self.assertEqual(review["summary"]["l3_context_action_total"], 5)
        self.assertTrue(all(item["disposition"] for item in review["candidates"]))
        self.assertTrue(all(item["disposition"] for item in review["actions"]))
        manifest_claims = {
            (variant["l3_scenario_id"], action_id)
            for item in self.contracts
            for variant in item["coverage"]["source_variants"]
            if variant["kind"] == "diagnostic_candidate"
            for action_id in variant["action_ids"]
        }
        ledger_claims = {
            (item["source_l3_id"], item["action_id"])
            for item in review["actions"]
            if item["disposition"] == "covered_as_capability_variant"
        }
        self.assertEqual(ledger_claims, manifest_claims)
        context_manifest_claims = {
            (variant["l3_scenario_id"], action_id)
            for item in self.contracts
            for variant in item["coverage"]["source_variants"]
            if variant["kind"] == "diagnostic_context"
            for action_id in variant["action_ids"]
        }
        context_ledger_claims = {
            (item["source_l3_id"], item["action_id"])
            for item in review["l3_context_actions"]
        }
        self.assertEqual(context_ledger_claims, context_manifest_claims)
        scenario_ids = {item["scenario_id"] for item in self.contracts}
        self.assertTrue(
            all(
                set(item["generated_scenario_ids"]) <= scenario_ids
                for item in review["candidates"]
            )
        )
        self.assertEqual(obligations["summary"]["obligation_total"], 389)
        self.assertEqual(obligations["summary"]["unresolved_count"], 0)
        self.assertTrue(
            all(item["disposition"] for item in obligations["obligations"])
        )
        self.assertTrue(
            all(
                set(item["scenario_ids"]) <= scenario_ids
                for item in obligations["obligations"]
            )
        )

    def test_full_review_is_idempotent_and_print_variants_are_preserved(self):
        rebuilt, review, obligations = build_full_review(self.manifest)
        self.assertEqual(rebuilt, self.manifest)
        self.assertEqual(review["summary"]["accepted_l1_count"], 12)
        self.assertEqual(obligations["summary"]["unresolved_count"], 0)
        self.assertTrue(
            all(
                datetime.fromisoformat(item["start_at"]).microsecond == 0
                for item in self.contracts
            )
        )
        printing = next(
            item
            for item in self.contracts
            if item["behavior_family"] == "printer_control"
        )
        self.assertTrue(
            any(
                "_l3_18_" in variant["l3_scenario_id"]
                for variant in printing["coverage"]["source_variants"]
            )
        )

    def test_all_oracles_replay_from_fresh_initial_state(self):
        for contract in self.contracts:
            with self.subTest(scenario_id=contract["scenario_id"]):
                scenario_class = _scenario_class(contract)
                build_engine = Engine(None, scenario_class())
                oracle = build_engine.build_oracle_workflow(run_oracle=False)
                replay_engine = Engine(None, scenario_class())
                replayed = replay_engine.replay_workflow(oracle)
                report = replay_engine.evaluation_report(replayed)
                self.assertTrue(
                    report["validation"]["success"],
                    report["validation"]["rationale"],
                )
                self.assertEqual(
                    report["validation"]["metadata"]["source_l3_id"],
                    contract["source"].get("l3_scenario_id"),
                )

    def test_do_nothing_and_missing_business_action_fail(self):
        for contract in self.contracts:
            with self.subTest(scenario_id=contract["scenario_id"]):
                scenario_class = _scenario_class(contract)

                idle_engine = Engine(None, scenario_class())
                idle_engine.scenario.setup()
                idle_engine._register_scenario_apps()
                idle_report = idle_engine.evaluation_report()
                self.assertFalse(idle_report["validation"]["success"])

                build_engine = Engine(None, scenario_class())
                oracle = build_engine.build_oracle_workflow(run_oracle=False)
                incomplete = copy.deepcopy(oracle)
                meaningful = set(
                    contract["validation"].get("meaningful_source_action_ids", [])
                )
                meaningful_step_ids = {
                    step["id"]
                    for step in contract["oracle_steps"]
                    if meaningful & set(step.get("source_action_ids", []))
                }
                missing_step = next(
                    name
                    for name, step in incomplete.dag.items()
                    if step.tool_name in WRITE_TOOLS
                    and (not meaningful_step_ids or name in meaningful_step_ids)
                )
                incomplete.dag.pop(missing_step)
                replay_engine = Engine(None, scenario_class())
                replayed = replay_engine.replay_workflow(incomplete)
                report = replay_engine.evaluation_report(replayed)
                self.assertFalse(report["validation"]["success"])

    def test_completion_query_must_follow_business_actions(self):
        for contract in self.contracts:
            with self.subTest(scenario_id=contract["scenario_id"]):
                scenario_class = _scenario_class(contract)
                build_engine = Engine(None, scenario_class())
                oracle = build_engine.build_oracle_workflow(run_oracle=False)
                without_confirmation = copy.deepcopy(oracle)
                steps = list(without_confirmation.dag.items())
                action_positions = [
                    index
                    for index, (_, step) in enumerate(steps)
                    if step.tool_name in WRITE_TOOLS
                ]
                last_action = max(action_positions)
                confirmation_tools = set(contract["validation"]["confirmation_tools"])
                remove = [
                    name
                    for index, (name, step) in enumerate(steps)
                    if index > last_action and step.tool_name in confirmation_tools
                ]
                self.assertTrue(remove)
                for name in remove:
                    without_confirmation.dag.pop(name)
                replay_engine = Engine(None, scenario_class())
                replayed = replay_engine.replay_workflow(without_confirmation)
                report = replay_engine.evaluation_report(replayed)
                self.assertFalse(report["validation"]["success"])
                self.assertFalse(
                    report["validation"]["metadata"]["conditions"][
                        "completion_observed_after_actions"
                    ]
                )

    def test_meeting_booking_accepts_model_chosen_request_id(self):
        contract = next(
            item
            for item in self.contracts
            if item["validation"]["kind"] == "meeting_booking"
        )
        self.assertNotIn("reservation_owner_id", contract["validation"])
        scenario_class = _scenario_class(contract)
        oracle = Engine(None, scenario_class()).build_oracle_workflow(
            run_oracle=False
        )
        alternative = copy.deepcopy(oracle)
        create_step = next(
            step
            for step in alternative.dag.values()
            if step.tool_name == "ScheduleApp__create_meeting"
        )
        create_step.tool_args["request_id"] = "model-chosen-request-id"

        replay_engine = Engine(None, scenario_class())
        replayed = replay_engine.replay_workflow(alternative)
        report = replay_engine.evaluation_report(replayed)

        self.assertTrue(
            report["validation"]["success"],
            report["validation"]["rationale"],
        )

    def test_meeting_prompts_publish_required_inputs_and_confirmation(self):
        contracts = {
            item["validation"]["kind"]: item
            for item in self.contracts
            if item["validation"]["kind"]
            in {"meeting_booking", "meeting_change"}
        }
        for contract in contracts.values():
            self.assertNotIn("reservation_owner_id", contract["validation"])
            self.assertIn("有效预约", contract["task"]["prompt"])

        change = contracts["meeting_change"]
        prompt = change["task"]["prompt"]
        expected = change["validation"]["expected_meeting"]
        self.assertIn("existing-001", prompt)
        self.assertIn("staff-01", prompt)
        self.assertIn(str(expected["expected_attendees"]), prompt)
        self.assertIn("2026-09-10 14:00 至 15:00", prompt)
        self.assertIn(expected["participant_ids"][0], prompt)
        self.assertEqual(expected["required_capabilities"], ["projector"])
        self.assertIn("投影设备", prompt)

    def test_device_state_accepts_per_target_state_confirmation(self):
        for contract in self.contracts:
            if contract["validation"]["kind"] != "device_state":
                continue
            with self.subTest(scenario_id=contract["scenario_id"]):
                scenario_class = _scenario_class(contract)
                oracle = Engine(
                    None, scenario_class()
                ).build_oracle_workflow(run_oracle=False)
                alternative = copy.deepcopy(oracle)
                steps = list(alternative.dag.items())
                action_positions = [
                    index
                    for index, (_, step) in enumerate(steps)
                    if step.tool_name in WRITE_TOOLS
                ]
                last_action = max(action_positions)
                overview_steps = [
                    name
                    for index, (name, step) in enumerate(steps)
                    if index > last_action
                    and step.tool_name
                    == "BuildingWorldApp__get_building_overview"
                ]
                self.assertTrue(overview_steps)
                for name in overview_steps:
                    alternative.dag.pop(name)
                target_ids = sorted(
                    {
                        step["args"]["device_id"]
                        for step in contract["oracle_steps"]
                        if step["tool"] in WRITE_TOOLS
                        and "device_id" in step.get("args", {})
                    }
                )
                for index, device_id in enumerate(target_ids, start=1):
                    alternative.add_node(
                        WorkflowStep(
                            name=f"confirm_target_device_{index:02d}",
                            op_type="TOOL",
                            tool_name="DeviceRegistryApp__get_device_state",
                            tool_args={"device_id": device_id},
                        )
                    )

                replay_engine = Engine(None, scenario_class())
                replayed = replay_engine.replay_workflow(alternative)
                report = replay_engine.evaluation_report(replayed)

                self.assertTrue(
                    report["validation"]["success"],
                    report["validation"]["rationale"],
                )

    def test_plain_assistant_completion_counts_as_report(self):
        for contract in self.contracts:
            with self.subTest(scenario_id=contract["scenario_id"]):
                scenario_class = _scenario_class(contract)
                oracle = Engine(
                    None, scenario_class()
                ).build_oracle_workflow(run_oracle=False)
                alternative = copy.deepcopy(oracle)
                report_steps = [
                    name
                    for name, step in alternative.dag.items()
                    if step.tool_name
                    == "AgentUserInterface__send_message_to_user"
                ]
                self.assertTrue(report_steps)
                for name in report_steps:
                    alternative.dag.pop(name)
                alternative.add_node(
                    WorkflowStep(
                        name="assistant_final_report",
                        op_type="ASSISTANT",
                        content=contract["task"]["completion_report"],
                    )
                )

                replay_engine = Engine(None, scenario_class())
                replayed = replay_engine.replay_workflow(alternative)
                report = replay_engine.evaluation_report(replayed)

                self.assertTrue(
                    report["validation"]["success"],
                    report["validation"]["rationale"],
                )


if __name__ == "__main__":
    unittest.main()
