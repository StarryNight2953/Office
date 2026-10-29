"""Run offline with python -m unittest discover -s tests -p 'test_visualization_scenes.py'."""

import tempfile
import unittest

from pydantic import ValidationError

from fairy.visualization.models import Scene
from fairy.visualization.scenes import SceneStore, catalog, default_scene, validate_scene


class SceneTests(unittest.TestCase):
    def test_all_existing_rooms_have_real_device_bindings(self):
        for room in catalog()["rooms"]:
            scene = default_scene(room["id"])
            bound = {o.device_id for o in scene.objects if o.device_id}
            self.assertEqual(bound, {d["device_id"] for d in room["devices"]})

    def test_layout_round_trip_keeps_initial_states_separate(self):
        scene = default_scene("k1315")
        scene.objects[0].position = (2.5, 0, -1.75)
        with tempfile.TemporaryDirectory() as directory:
            store = SceneStore(directory)
            store.save(scene.id, scene)
            restored = store.load(scene.id)
            self.assertEqual(scene.model_dump(), restored.model_dump())
            self.assertNotIn("device_states", restored.model_dump())

    def test_shared_circuit_is_valid_but_unknown_or_wrong_bindings_fail(self):
        scene = default_scene("k1315")
        lights = [o for o in scene.objects if o.kind == "lighting"]
        self.assertEqual(lights[0].device_id, lights[1].device_id)
        validate_scene(scene)
        lights[0].device_id = "unknown"
        with self.assertRaisesRegex(ValueError, "unknown device"):
            validate_scene(scene)
        lights[0].device_id = "k1315_hvac_01"
        with self.assertRaisesRegex(ValueError, "wrong kind"):
            validate_scene(scene)

    def test_duplicate_ids_and_nonfinite_positions_fail(self):
        raw = default_scene("k1315").model_dump()
        raw["objects"][1]["id"] = raw["objects"][0]["id"]
        with self.assertRaises(ValidationError):
            Scene.model_validate(raw)
        raw = default_scene("k1315").model_dump()
        raw["objects"][0]["position"] = [float("nan"), 0, 0]
        with self.assertRaises(ValidationError):
            Scene.model_validate(raw)

    def test_initial_settings_validated_before_persistence(self):
        scene = default_scene("k1315")
        scene.initial_states["k1315_hvac_01"]["target_temperature_c"] = 100
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValidationError):
                SceneStore(directory).save(scene.id, scene)

    def test_room_and_model_paths_cannot_escape_local_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyError):
                SceneStore(directory).load("../../secrets")
        raw = default_scene("k1315").model_dump()
        raw["objects"][0]["asset_url"] = "/models/../../private.glb"
        with self.assertRaises(ValidationError):
            Scene.model_validate(raw)


if __name__ == "__main__":
    unittest.main()
