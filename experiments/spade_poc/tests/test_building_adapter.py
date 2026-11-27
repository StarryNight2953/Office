import tempfile
import unittest
from pathlib import Path

from office_spade.building import BuildingBoundary


class BuildingAdapterContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="office-building-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.boundary = BuildingBoundary(Path(self.temporary.name))

    def hvac_id(self, room):
        return next(
            device["device_id"]
            for device in self.boundary.snapshot(room)["context"]["devices"]
            if device["device_type"] == "hvac"
        )

    def arguments(self, device_id, target=24.0):
        return {
            "device_id": device_id,
            "power_on": True,
            "mode": "cooling",
            "target_temperature_c": target,
            "fan_level": 3,
        }

    def test_room_guard_rejects_foreign_device_without_a_write(self):
        foreign = self.hvac_id("K1316")
        before = self.boundary.snapshot("K1316")
        with self.assertRaises(PermissionError):
            self.boundary.invoke("K1315", "HvacApp__set_hvac", self.arguments(foreign), "foreign")
        self.assertEqual(before, self.boundary.snapshot("K1316"))

    def test_rpc_retry_is_idempotent_and_conflicting_id_is_rejected(self):
        device_id = self.hvac_id("K1315")
        args = self.arguments(device_id)
        first = self.boundary.invoke("K1315", "HvacApp__set_hvac", args, "one-call")
        events = len(self.boundary.world.events)
        second = self.boundary.invoke("K1315", "HvacApp__set_hvac", args, "one-call")
        self.assertEqual(first, second)
        self.assertEqual(events, len(self.boundary.world.events))
        with self.assertRaisesRegex(ValueError, "different arguments"):
            self.boundary.invoke(
                "K1315", "HvacApp__set_hvac", self.arguments(device_id, target=25), "one-call"
            )

    def test_clock_advances_only_explicitly_and_is_shared_between_rooms(self):
        first = self.boundary.snapshot("K1315")
        self.boundary.invoke(
            "K1315", "HvacApp__set_hvac", self.arguments(self.hvac_id("K1315")), "setpoint"
        )
        unchanged = self.boundary.snapshot("K1315")
        self.assertEqual(first["observations"], unchanged["observations"])
        self.assertEqual(first["simulation_time"], unchanged["simulation_time"])
        self.boundary.advance(60)
        one, two = self.boundary.snapshot("K1315"), self.boundary.snapshot("K1316")
        self.assertEqual(one["simulation_time"], two["simulation_time"])
        self.assertNotEqual(first["simulation_time"], one["simulation_time"])


if __name__ == "__main__":
    unittest.main()
