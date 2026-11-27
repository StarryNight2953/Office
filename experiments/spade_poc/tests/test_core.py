import json
import unittest

from office_spade.core import Envelope, RoomState


class RoomContracts(unittest.TestCase):
    def test_duplicate_write_replays_original_result_without_mutating_state(self):
        room = RoomState("K1315")
        original = Envelope(
            "first", "K1315", "task", {"operation": "set_test_temperature", "temperature_c": 28}
        )
        first, duplicate = room.execute(original)
        self.assertFalse(duplicate)
        room.execute(
            Envelope(
                "second",
                "K1315",
                "task",
                {"operation": "set_test_temperature", "temperature_c": 30},
            )
        )
        replay, duplicate = room.execute(original)
        self.assertTrue(duplicate)
        self.assertEqual(replay, first)
        self.assertEqual(room.temperature_c, 30)
        self.assertEqual(room.total_executions, 2)
        replay["state"]["temperature_c"] = 99
        self.assertEqual(room.execute(original)[0]["state"]["temperature_c"], 28)

    def test_task_id_conflict_rejected_without_second_write(self):
        room = RoomState("K1315")
        room.execute(Envelope("same", "K1315", "task", {"operation": "get_state"}))
        with self.assertRaisesRegex(ValueError, "different payload"):
            room.execute(
                Envelope(
                    "same",
                    "K1315",
                    "task",
                    {"operation": "set_test_temperature", "temperature_c": 30},
                )
            )
        self.assertEqual(room.temperature_c, 27)
        self.assertEqual(room.total_executions, 1)

    def test_cross_room_and_invalid_writes_do_not_change_state(self):
        room = RoomState("K1315")
        with self.assertRaisesRegex(ValueError, "scope violation"):
            room.execute(Envelope("wrong", "K1316", "task", {"operation": "get_state"}))
        for value in (True, "30", float("inf"), -5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                room.execute(
                    Envelope(
                        "invalid",
                        "K1315",
                        "task",
                        {"operation": "set_test_temperature", "temperature_c": value},
                    )
                )
        self.assertEqual(room.temperature_c, 27)
        self.assertEqual(room.total_executions, 0)

    def test_malformed_messages_fail_closed(self):
        for body in ("[]", "{}", "null", "not json", "x" * 32769):
            with self.subTest(body=body[:30]), self.assertRaises(ValueError):
                Envelope.decode(body)
        message = json.loads(Envelope("valid", "K1315", "task", {}).encode())
        for key, value in (
            ("schema_version", 2),
            ("schema_version", True),
            ("room_id", "K1324"),
            ("type", "unknown"),
            ("payload", []),
            ("message_id", ""),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Envelope.decode(json.dumps({**message, key: value}))


if __name__ == "__main__":
    unittest.main()
