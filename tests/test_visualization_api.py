"""End-to-end API checks use real FAIRY physics and an in-process ASGI client."""

import tempfile
import unittest
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from fairy.visualization.server import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(self.directory.name))
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def create_run(self, room_id="k1315"):
        response = self.client.post("/api/runs", json={"scene_id": room_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_catalog_and_layout_save_reload(self):
        with patch.dict(os.environ, {}, clear=True):
            catalog = self.client.get("/api/catalog").json()
        rooms = catalog["rooms"]
        self.assertFalse(catalog["agent_configured"])
        self.assertEqual(catalog["agent_readiness"]["reason_code"], "missing_model")
        self.assertEqual(catalog["agent_configured"], catalog["agent_readiness"]["configured"])
        self.assertEqual({room["id"] for room in rooms}, {"k1315", "k1316", "k1324"})
        scene = self.client.get("/api/scenes/k1315").json()
        scene["objects"][0]["position"] = [1.75, 0, -0.5]
        response = self.client.put("/api/scenes/k1315", json=scene)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/scenes/k1315").json(), response.json())

    def test_rejected_command_and_confirmed_websocket_state(self):
        state = self.create_run()
        run_id = state["run_id"]
        device_id = "k1315_lighting_front_01"
        with self.client.websocket_connect(f"/api/runs/{run_id}/events") as socket:
            self.assertEqual(socket.receive_json()["data"], state)
            response = self.client.post(f"/api/runs/{run_id}/commands", json={
                "device_id": device_id,
                "parameters": {"power_on": True, "brightness_pct": 0},
            })
            self.assertEqual(response.json()["status"], "rejected")
            rejected = socket.receive_json()["data"]
            self.assertFalse(rejected["devices"][device_id]["power_on"])
            response = self.client.post(f"/api/runs/{run_id}/commands", json={
                "device_id": device_id,
                "parameters": {"power_on": True, "brightness_pct": 65},
            })
            self.assertEqual(response.json()["status"], "accepted")
            confirmed = socket.receive_json()["data"]
            self.assertTrue(confirmed["devices"][device_id]["power_on"])
            self.assertEqual(confirmed["devices"][device_id]["settings"]["brightness_pct"], 65)
        with self.client.websocket_connect(f"/api/runs/{run_id}/events") as socket:
            self.assertEqual(socket.receive_json()["data"], confirmed)

    def test_physics_advances_and_reset_reloads_saved_initial_state(self):
        scene = self.client.get("/api/scenes/k1315").json()
        scene["initial_states"]["k1315_lighting_front_01"]["power_on"] = True
        self.assertEqual(self.client.put("/api/scenes/k1315", json=scene).status_code, 200)
        initial = self.create_run()
        run_id = initial["run_id"]
        hvac = "k1315_hvac_01"
        response = self.client.post(f"/api/runs/{run_id}/commands", json={
            "device_id": hvac,
            "parameters": {"power_on": True, "mode": "cooling", "target_temperature_c": 22, "fan_level": 3},
        })
        self.assertEqual(response.json()["status"], "accepted")
        command_state = self.client.get(f"/api/runs/{run_id}").json()
        self.assertEqual(command_state["sensors"], initial["sensors"])
        evolved = self.client.post(f"/api/runs/{run_id}/advance", json={"seconds": 600}).json()
        self.assertNotEqual(evolved["sim_time"], initial["sim_time"])
        self.assertNotEqual(evolved["sensors"], initial["sensors"])
        self.assertGreater(evolved["metrics"]["energy_kwh"], 0)
        for forbidden in ("truth", "truth_value", "zone_states", "rng_state"):
            self.assertNotIn(forbidden, str(evolved))
        reset = self.client.post(f"/api/runs/{run_id}/reset").json()
        self.assertNotEqual(reset["run_id"], run_id)
        self.assertEqual(reset["sim_time"], initial["sim_time"])
        self.assertEqual(reset["devices"], initial["devices"])
        self.assertEqual(self.client.get(f"/api/runs/{run_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/runs/current").json(), reset)

    def test_bad_scene_cannot_replace_saved_layout_or_active_run(self):
        state = self.create_run()
        original = self.client.get("/api/scenes/k1315").json()
        invalid = self.client.get("/api/scenes/k1315").json()
        invalid["objects"][0]["device_id"] = "foreign_device"
        response = self.client.put("/api/scenes/k1315", json=invalid)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/scenes/k1315").json(), original)
        self.assertEqual(self.client.post("/api/runs", json={"scene_id": "missing"}).status_code, 422)
        self.assertEqual(self.client.get("/api/runs/current").json(), state)


if __name__ == "__main__":
    unittest.main()
