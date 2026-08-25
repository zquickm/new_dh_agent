import unittest

from agents.tools.map_action import execute_map_action


class MapActionToolTest(unittest.TestCase):
    def test_returns_standard_client_event(self):
        output = execute_map_action(
            "fly_to",
            {"center": [121.5, 31.2], "zoom": 10},
        )

        self.assertEqual(output.content["action_type"], "FLY_TO")
        self.assertEqual(
            output.client_events,
            (
                {
                    "action_type": "FLY_TO",
                    "payload": {"center": [121.5, 31.2], "zoom": 10},
                },
            ),
        )

    def test_clear_accepts_empty_payload(self):
        output = execute_map_action("CLEAR", {})

        self.assertEqual(output.client_events[0]["action_type"], "CLEAR")

    def test_rejects_unknown_action(self):
        with self.assertRaisesRegex(ValueError, "不支持的地图动作"):
            execute_map_action("layer.upsert", {})

    def test_rejects_missing_required_payload_field(self):
        with self.assertRaisesRegex(ValueError, "center"):
            execute_map_action("FLY_TO", {"zoom": 10})

    def test_rejects_non_object_payload(self):
        with self.assertRaisesRegex(ValueError, "payload 必须为对象"):
            execute_map_action("CLEAR", [])
