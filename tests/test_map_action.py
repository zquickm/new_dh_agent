import unittest

from agents.tools.map_action import TOOL_DESCRIPTION, execute_map_action


class MapActionToolTest(unittest.TestCase):
    def test_description_forbids_duplicate_station_rendering(self):
        self.assertIn("查询、输出或显示观测站点时禁止调用本工具", TOOL_DESCRIPTION)
        self.assertIn("observation_service 的站点查询和单站分析", TOOL_DESCRIPTION)
        self.assertIn("不得再调用本工具重复清图、标点", TOOL_DESCRIPTION)

    def test_fly_to_only_moves_the_view(self):
        output = execute_map_action(
            "fly_to",
            {
                "center": [121.5, 31.2],
                "zoom": 10,
            },
        )

        self.assertEqual(output.content["action_type"], "FLY_TO")
        self.assertEqual(
            output.client_events,
            ({
                "action_type": "FLY_TO",
                "payload": {"center": [121.5, 31.2], "zoom": 10},
            },),
        )

    def test_fly_to_accepts_coordinates_alias_and_adds_default_zoom(self):
        output = execute_map_action("FLY_TO", {"coordinates": [121.5, 31.2]})

        self.assertEqual(
            output.client_events,
            ({
                "action_type": "FLY_TO",
                "payload": {"center": [121.5, 31.2], "zoom": 12},
            },),
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
