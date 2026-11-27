"""Business-boundary and negative-control tests, runnable with unittest."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime
from unittest.mock import patch

from smartoffice.controllers.engine import Engine
from smartoffice.scenarios.building_kechuang.l3.scenarios import L3_SCENARIO_CLASSES
from scripts.diagnose_building_l3_to_l1 import (
    analyze_source, print_verdict, probe_printing,
)


class BuildingL1DiagnosticTests(unittest.TestCase):
    def test_all_source_actions_are_accounted_for_without_quota(self):
        counts = []
        for scenario_class in L3_SCENARIO_CLASSES:
            with self.subTest(number=scenario_class.spec.number):
                report, _ = analyze_source(scenario_class)
                self.assertTrue(report["static_integrity_passed"], report["static_errors"])
                self.assertEqual(report["action_assignment_ratio"], 1.0)
                self.assertFalse(report["fully_split_and_validated"])
                actions = [step for item in report["candidates"] for step in item["source_action_ids"]]
                self.assertEqual(len(actions), len(set(actions)))
                counts.append(len(report["candidates"]))
        self.assertGreater(max(counts), 6)
        self.assertGreater(len(set(counts)), 1)

    def test_printing_crosses_checkpoints_and_preserves_shared_queue_group(self):
        report, _ = analyze_source(L3_SCENARIO_CLASSES[17])
        printing = [item for item in report["candidates"]
                    if item["behavior_family"] == "printing_fulfillment"]
        self.assertEqual([item["batch_indices"] for item in printing], [[1], [2, 3]])
        self.assertIn("submit_print_batch_02", printing[1]["source_action_ids"])
        self.assertIn("submit_print_batch_03", printing[1]["source_action_ids"])
        self.assertIn("verify_print_deadline_02", printing[1]["source_evidence_ids"])
        self.assertIn("verify_print_deadline_03", printing[1]["source_evidence_ids"])
        self.assertGreater(printing[1]["source_evidence_end_minute"], printing[1]["start_minute"])
        self.assertEqual(report["action_ownership"]["shutdown_shared_printer"],
                         "l3_context:shared_printer_closeout")

    def test_environment_feedback_stays_with_multi_device_control(self):
        report, _ = analyze_source(L3_SCENARIO_CLASSES[5])
        feedback = [item for item in report["candidates"]
                    if any(step.startswith("feedback_") for step in item["source_action_ids"])]
        self.assertTrue(feedback)
        for item in feedback:
            methods = {step["method"] for step in item["source_actions"]}
            self.assertIn("set_hvac", methods)
            self.assertIn("set_ventilation", methods)
            self.assertTrue(any(step.startswith("verify_stability_")
                                for step in item["source_evidence_ids"]))
            self.assertEqual(item["status"], "needs_review")

    def test_power_constraint_and_automatic_interaction_are_not_lost(self):
        report, _ = analyze_source(L3_SCENARIO_CLASSES[15])
        self.assertTrue(all(item["behavior_family"] == "power_constrained_adjustment"
                            for item in report["candidates"]))
        self.assertTrue(all(item["power_limit_w"] == 4200.0 for item in report["candidates"]))
        changed, _ = analyze_source(L3_SCENARIO_CLASSES[10])
        event = next(item for item in changed["business_obligations"] if item["kind"] == "change_response")
        self.assertEqual(event["status"], "needs_review")
        self.assertIn("自动", event["environment_responsibility"])

    def test_missing_or_wrong_completion_tool_cannot_pass_static_audit(self):
        scenario_class = L3_SCENARIO_CLASSES[17]
        workflow = Engine(None, scenario_class()).build_oracle_workflow(run_oracle=False)
        workflow.dag["verify_print_deadline_02"].tool_name = "BuildingWorldApp__get_building_overview"
        with patch.object(Engine, "build_oracle_workflow", return_value=workflow):
            report, _ = analyze_source(scenario_class)
        self.assertFalse(report["static_integrity_passed"])
        self.assertIn("source_tool_mismatch:verify_print_deadline_02", report["static_errors"])

    def test_print_prototypes_reject_incomplete_and_incorrect_arms(self):
        # Includes initially visible requests, later user-revealed requests,
        # one-job rounds and a round with two serialized jobs.
        for number in (13, 18):
            with self.subTest(number=number):
                scenario_class = L3_SCENARIO_CLASSES[number - 1]
                report, steps = analyze_source(scenario_class)
                probes = probe_printing(scenario_class, report, steps)
                self.assertTrue(probes)
                self.assertTrue(all(item["prototype_contrast_passed"] for item in probes))
                if number == 18:
                    self.assertTrue(probes[1]["background_job_ids"])
                    jobs = probes[1]["outcomes"]["correct"]["target_jobs"]
                    self.assertGreaterEqual(jobs[1]["started_at_timestamp"], jobs[0]["ready_at_timestamp"])
                    self.assertGreater(jobs[0]["priority"], jobs[1]["priority"])

    def test_business_verdict_checks_deadline_duplicates_and_no_confirmation(self):
        request = {"request_id": "request-a", "job_id": "job-a", "document_name": "agenda",
                   "copies": 2, "pages_per_copy": 3, "priority": 2, "requested_by": "staff-01",
                   "status": "completed", "submit_at": "2026-09-23T08:00:00+00:00",
                   "ready_by": "2026-09-23T09:00:00+00:00"}
        deadline = datetime.fromisoformat(request["ready_by"]).timestamp()
        job = {**request, "submitted_at": request["submit_at"], "ready_at_timestamp": deadline - 1}
        self.assertTrue(print_verdict([request], [job], {"job-a"})["success"])
        self.assertFalse(print_verdict([request], [job], set())["success"])
        late = {**job, "ready_at_timestamp": deadline + 1}
        self.assertFalse(print_verdict([request], [late], {"job-a"})["success"])
        duplicate = {**copy.deepcopy(job), "job_id": "job-b", "request_id": None}
        self.assertFalse(print_verdict([request], [job, duplicate], {"job-a"})["success"])

    def test_multifamily_probes_distinguish_activation_from_temperature_goal(self):
        from scripts.building_l1_behavior_probes import probe_additional_behaviors
        analyzed = {number: analyze_source(L3_SCENARIO_CLASSES[number - 1]) for number in (6, 8, 16)}
        probes = {item["behavior_family"]: item for item in probe_additional_behaviors(analyzed)}
        for family in ("hvac_activation", "lighting_service", "meeting_equipment",
                       "room_recovery", "power_constrained_adjustment", "room_booking"):
            self.assertTrue(probes[family]["prototype_contrast_passed"], family)
        thermal = probes["environment_control"]
        self.assertFalse(thermal["prototype_contrast_passed"])
        checks = thermal["outcomes"]["source_reference"]["conditions"]
        self.assertTrue(checks["target_configuration"])
        self.assertFalse(checks["temperature_in_band_last_two_samples"])
        self.assertEqual(probes["room_booking"]["source_l3_id"], None)


if __name__ == "__main__":
    unittest.main()
