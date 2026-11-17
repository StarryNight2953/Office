"""Integration tests for the public visualization/runtime boundary."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from fairy.visualization.runner import Runner, agent_configuration


HVAC = "k1315_hvac_01"
LIGHT = "k1315_lighting_front_01"


def scene(**overrides):
    value = {"id": "k1315", "room_id": "k1315", "initial_states": {}, "objects": []}
    value.update(overrides)
    return value


def temperature(snapshot):
    return next(row["value"] for row in snapshot["sensors"] if row["quantity"] == "air_temperature_c")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()

    def runner(self, **overrides):
        runner = Runner(scene(**overrides))
        self.runners.append(runner)
        return runner

    def wait_until(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for worker")

    def test_commands_change_authoritative_state_but_not_temperature(self):
        runner = self.runner()
        initial = runner.snapshot()
        self.assertEqual(len(initial["devices"]), 9)
        self.assertEqual(len(initial["sensors"]), 4)
        result = runner.command(HVAC, {"power_on": True, "mode": "cooling", "target_temperature_c": 24.0, "fan_level": 3})
        self.assertEqual(result["status"], "accepted")
        after = runner.snapshot()
        self.assertTrue(after["devices"][HVAC]["power_on"])
        self.assertEqual(after["devices"][HVAC]["target_temperature_c"], 24)
        self.assertEqual(temperature(initial), temperature(after))
        evolved = runner.advance(600)
        self.assertLess(temperature(evolved), temperature(initial))
        self.assertNotEqual(temperature(evolved), 24)
        self.assertGreater(evolved["metrics"]["energy_kwh"], 0)
        elapsed = datetime.fromisoformat(evolved["sim_time"]) - datetime.fromisoformat(initial["sim_time"])
        self.assertEqual(elapsed.total_seconds(), 600)

    def test_rejections_do_not_mutate_device_state(self):
        runner = self.runner()
        before = runner.snapshot()["devices"]
        commands = [
            (HVAC, {"power_on": True, "target_temperature_c": 70}),
            (LIGHT, {"power_on": True, "brightness_pct": 0}),
            (HVAC, {"power_on": "false"}),
            (HVAC, {"target_temperature_c": float("nan")}),
            (HVAC, {"fan_level": True}),
            (LIGHT, {"unknown": 3}),
            ("missing", {"power_on": True}),
            ("k1315_printer_01", {"power_on": True}),
        ]
        for device, parameters in commands:
            with self.subTest(device=device, parameters=parameters):
                self.assertEqual(runner.command(device, parameters)["status"], "rejected")
                self.assertEqual(before, runner.snapshot()["devices"])

    def test_initial_configuration_and_fresh_reset(self):
        initial = {HVAC: {"power_on": True, "mode": "heating", "target_temperature_c": 26, "fan_level": 1}}
        runner = self.runner(initial_states=initial)
        first = runner.snapshot()
        self.assertEqual(first["devices"][HVAC]["mode"], "heating")
        self.assertEqual(first["metrics"]["energy_kwh"], 0)
        runner.command(HVAC, {"power_on": False})
        runner.advance(300)
        reset = self.runner(initial_states=initial).snapshot()
        self.assertNotEqual(first["run_id"], reset["run_id"])
        self.assertEqual(first["devices"], reset["devices"])
        self.assertEqual(first["sensors"], reset["sensors"])
        self.assertEqual(first["sim_time"], reset["sim_time"])
        self.assertEqual(initial[HVAC]["mode"], "heating")

    def test_invalid_initial_state_fails_without_a_worker_leak(self):
        with self.assertRaisesRegex(ValueError, "Invalid initial state"):
            Runner(scene(initial_states={HVAC: {"power_on": True, "target_temperature_c": 60}}))
        with self.assertRaisesRegex(ValueError, "unknown_room"):
            Runner(scene(room_id="../k1315"))

    def test_grouped_meshes_do_not_duplicate_devices_or_energy(self):
        one = self.runner(initial_states={LIGHT: {"power_on": True, "brightness_pct": 75}}, objects=[{"id": "a", "device_id": LIGHT}])
        many = self.runner(initial_states={LIGHT: {"power_on": True, "brightness_pct": 75}}, objects=[{"id": str(i), "device_id": LIGHT} for i in range(8)])
        first = one.advance(600)
        second = many.advance(600)
        self.assertEqual(first["devices"], second["devices"])
        self.assertEqual(first["metrics"], second["metrics"])

    def test_snapshot_is_detached_json_and_excludes_hidden_truth(self):
        runner = self.runner()
        snapshot = runner.snapshot()
        encoded = json.dumps(snapshot, allow_nan=False)
        for forbidden in ("truth", "rng", "checkpoint", "initial_zone_states", "api_key"):
            self.assertNotIn(forbidden, encoded)
        snapshot["devices"][HVAC]["power_on"] = True
        snapshot["events"].clear()
        current = runner.snapshot()
        self.assertFalse(current["devices"][HVAC]["power_on"])
        self.assertTrue(current["events"])
        time.sleep(0.02)
        self.assertEqual(current, runner.snapshot())

    def test_manual_commands_are_serviced_during_demo_and_stop_is_cooperative(self):
        runner = self.runner()
        started = runner.start_agent("demo")
        self.assertEqual(started["agent"]["status"], "running")
        result = runner.command(LIGHT, {"power_on": True, "brightness_pct": 50})
        self.assertEqual(result["status"], "accepted")
        runner.stop_agent()
        self.wait_until(lambda: runner.snapshot()["agent"]["status"] == "stopped")
        self.assertTrue(any(row["kind"] == "agent_stopped" for row in runner.snapshot()["events"]))

    def test_demo_repeats_with_identical_observations_after_three_fresh_runs(self):
        snapshots = []
        for _ in range(3):
            runner = self.runner()
            runner.start_agent("demo")
            self.wait_until(lambda: runner.snapshot()["agent"]["status"] == "completed")
            snapshot = runner.snapshot()
            self.assertGreater(snapshot["metrics"]["energy_kwh"], 0)
            self.assertTrue(any(row["kind"] == "demo_observation" for row in snapshot["events"]))
            self.assertTrue(any("deterministic" in row["message"] for row in snapshot["events"]))
            snapshots.append(snapshot)
            runner.close()
        self.assertEqual(len({item["run_id"] for item in snapshots}), 3)
        for snapshot in snapshots[1:]:
            for field in ("sim_time", "devices", "sensors", "metrics", "events", "agent"):
                self.assertEqual(snapshots[0][field], snapshot[field], field)

    def test_concurrent_commands_are_serial_and_events_bounded(self):
        runner = self.runner()
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda value: runner.command(LIGHT, {"power_on": True, "brightness_pct": value}), [20, 40, 60, 80] * 55))
        self.assertTrue(all(row["status"] == "accepted" for row in responses))
        events = runner.snapshot()["events"]
        self.assertEqual(len(events), 200)
        ids = [int(row["id"].removeprefix("ui-")) for row in events]
        self.assertEqual(ids, sorted(set(ids)))

    def test_close_rejects_future_work_and_missing_llm_configuration_is_clear(self):
        runner = self.runner()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OFFICE_LLM_MODEL"):
                runner.start_agent("llm")
        runner.start_agent("demo")
        runner.close()
        with self.assertRaisesRegex(RuntimeError, "run_closed"):
            runner.command(LIGHT, {"power_on": True})
        self.wait_until(lambda: not runner._worker.is_alive())

    def test_time_advance_validation(self):
        runner = self.runner()
        before = runner.snapshot()["sim_time"]
        for value in (0, -1, 0.5, 3601, True, float("inf"), float("nan")):
            with self.subTest(seconds=value), self.assertRaises(ValueError):
                runner.advance(value)
        self.assertEqual(before, runner.snapshot()["sim_time"])

    def test_agent_errors_are_classified_without_provider_content(self):
        runner = self.runner()
        authentication_error = type("AuthenticationError", (Exception,), {})
        rate_limit_error = type("RateLimitError", (Exception,), {})
        examples = [(TimeoutError, "timeout"), (authentication_error, "authentication"),
                    (rate_limit_error, "rate_limit"), (ConnectionError, "connection"),
                    (ImportError, "dependency"), (RuntimeError, "execution")]
        for error_type, code in examples:
            with self.subTest(code=code), patch("fairy.visualization.runner.agent_configuration", return_value={"configured": True}), patch.object(runner, "_run_llm", side_effect=error_type("private-provider-body test-secret")):
                runner.start_agent("llm", "Observe the room")
                self.wait_until(lambda: runner.snapshot()["agent"]["status"] == "error")
                snapshot = runner.snapshot()
                self.assertEqual(snapshot["agent"]["error_code"], code)
                self.assertNotIn("private-provider-body", json.dumps(snapshot))
                self.assertNotIn("test-secret", json.dumps(snapshot))
                self.assertNotIn("summary", snapshot["agent"])

    def test_real_fairy_agent_tool_loop_with_an_offline_provider(self):
        """Use the real Agent loop, substituting only its external model client."""
        runner = self.runner()
        module = ModuleType("fairy.agents.llm.openai_compatible_client")

        class FakeClient:
            provider = "openai"
            max_output_tokens = 200

            def __init__(self, model, **kwargs):
                self.model = model
                self.api_client = self
                self.turn = 0

            def with_options(self, **kwargs):
                return self

            def close(self):
                pass

            def chat_completion(self, messages, tools=None):
                steps = [
                    ("HvacApp__set_hvac", {"device_id": HVAC, "power_on": True, "mode": "cooling", "target_temperature_c": 24., "fan_level": 3}),
                    ("SystemApp__advance_time", {"seconds": 600, "stop_on_event": False}),
                    ("BuildingSensorApp__read_all_sensors", {}),
                ]
                calls = []
                if self.turn < len(steps):
                    name, arguments = steps[self.turn]
                    calls = [SimpleNamespace(id=f"tool-{self.turn}", type="function", function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))]
                self.turn += 1
                message = SimpleNamespace(role="assistant", content="已完成观测；test-secret" if not calls else None, tool_calls=calls)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)

        module.OpenAICompatibleClient = FakeClient
        with patch.dict(sys.modules, {module.__name__: module}), patch.dict(os.environ, {"OFFICE_LLM_MODEL": "test-model", "OFFICE_LLM_API_KEY": "test-secret"}), patch("fairy.visualization.runner.agent_configuration", return_value={"configured": True}):
            runner.start_agent("llm", "Cool the room and observe the result.")
            self.wait_until(lambda: runner.snapshot()["agent"]["status"] != "running")
        snapshot = runner.snapshot()
        self.assertEqual(snapshot["agent"]["status"], "completed", snapshot["agent"])
        self.assertEqual(snapshot["agent"]["summary"], "已完成观测；[redacted]")
        self.assertTrue(snapshot["devices"][HVAC]["power_on"])
        self.assertGreater(snapshot["metrics"]["energy_kwh"], 0)
        self.assertEqual(sum(event["kind"] == "tool_call_end" for event in snapshot["events"]), 3)
        self.assertNotIn("test-secret", json.dumps(snapshot))


class ReadinessTests(unittest.TestCase):
    def test_model_key_and_sdk_readiness_are_distinct_and_public(self):
        cases = [
            ({}, True, "missing_model"),
            ({"OFFICE_LLM_MODEL": "example-model"}, True, "missing_api_key"),
            ({"OFFICE_LLM_MODEL": "example-model", "OFFICE_LLM_API_KEY": "  "}, True, "missing_api_key"),
            ({"OFFICE_LLM_MODEL": "example-model", "OFFICE_LLM_API_KEY": "test-secret"}, False, "missing_sdk"),
            ({"OFFICE_LLM_MODEL": "example-model", "OPENAI_API_KEY": "test-secret"}, True, None),
        ]
        for variables, sdk, code in cases:
            with self.subTest(code=code), patch.dict(os.environ, variables, clear=True), patch("fairy.visualization.runner.importlib.util.find_spec", return_value=object() if sdk else None):
                result = agent_configuration()
                self.assertEqual(result["reason_code"], code)
                self.assertEqual(result["configured"], code is None)
                self.assertEqual(result["reason"] is None, code is None)
                self.assertNotIn("test-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
