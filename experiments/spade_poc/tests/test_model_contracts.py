import copy
import unittest

from office_spade.model import validate_expectations, verify_expectations


class CompletionContracts(unittest.TestCase):
    def setUp(self):
        self.state = {
            "context": {
                "devices": [
                    {
                        "device_id": "own-hvac",
                        "power_on": False,
                        "mode": "off",
                        "target_temperature_c": None,
                        "level": 0,
                        "settings": {},
                    }
                ]
            }
        }

    def test_text_claim_does_not_satisfy_device_contract(self):
        expected = [{"device_id": "own-hvac", "power_on": True, "target_temperature_c": 24}]
        validate_expectations(expected, self.state)
        self.assertFalse(verify_expectations(expected, self.state)["passed"])
        changed = copy.deepcopy(self.state)
        changed["context"]["devices"][0].update(power_on=True, target_temperature_c=24.0)
        self.assertTrue(verify_expectations(expected, changed)["passed"])

    def test_foreign_and_empty_contracts_rejected_before_model_calls(self):
        for expected in (
            [],
            [{"device_id": "foreign-hvac", "power_on": True}],
            [{"device_id": "own-hvac"}],
            [{"device_id": "own-hvac", "physical_temperature": 24}],
        ):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                validate_expectations(expected, self.state)

    def test_boolean_and_finite_numeric_fields_are_not_coerced(self):
        for expected in (
            [{"device_id": "own-hvac", "power_on": "true"}],
            [{"device_id": "own-hvac", "level": True}],
            [{"device_id": "own-hvac", "target_temperature_c": float("nan")}],
        ):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                validate_expectations(expected, self.state)


if __name__ == "__main__":
    unittest.main()
