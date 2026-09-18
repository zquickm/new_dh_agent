import unittest

from agents.tools.map_action import TOOL_DESCRIPTION, TOOL_PARAMETERS, execute_map_action


class MapActionToolTest(unittest.TestCase):
    def test_geojson_action_preserves_geometry_and_validates_envelope(self):
        geojson = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": 1, "properties": {"name": "航线"},
             "geometry": {"type": "MultiLineString", "coordinates": [[[122, 30], [123, 31]]]}},
        ]}
        payload = {"layer_id": "routes", "geojson": geojson, "replace": False, "fit_bounds": False}
        output = execute_map_action("GEOJSON_ADD", payload)
        self.assertEqual(output.client_events, ({"action_type": "GEOJSON_ADD", "payload": payload},))
        self.assertIn("GEOJSON_ADD", TOOL_PARAMETERS["properties"]["action_type"]["enum"])
        for invalid in (
            {"layer_id": ""}, {"group_id": 1}, {"replace": "false"}, {"fit_bounds": 1},
            {"geojson": []}, {"geojson": {"type": "FeatureCollection", "features": [1]}},
            {"geojson": {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "bad"}}]}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                execute_map_action("GEOJSON_ADD", {**payload, **invalid})

    def test_description_forbids_duplicate_station_rendering(self):
        self.assertIn("查询、输出或显示业务观测站点时禁止调用本工具", TOOL_DESCRIPTION)
        self.assertIn("断面测点地图展示除外", TOOL_DESCRIPTION)
        self.assertIn("observation_service 的站点查询和单站分析", TOOL_DESCRIPTION)
        self.assertIn("不得再调用本工具重复清图、标点", TOOL_DESCRIPTION)

    def test_fly_to_clears_old_layers_adds_marker_and_moves_the_view(self):
        output = execute_map_action(
            "fly_to",
            {
                "center": [121.5, 31.2],
                "zoom": 10,
                "marker_name": "目标站点",
            },
        )

        self.assertEqual(output.content["action_type"], "FLY_TO")
        self.assertEqual(
            [event["action_type"] for event in output.client_events],
            ["CLEAR", "MARKER_ADD", "FLY_TO"],
        )
        self.assertEqual(
            output.client_events[0]["payload"]["layer_ids"],
            ["observation-stations", "map-location-markers"],
        )
        self.assertEqual(
            output.client_events[1]["payload"]["markers"],
            [{
                "id": "fly-to-target",
                "name": "目标站点",
                "coordinates": [121.5, 31.2],
            }],
        )
        self.assertEqual(
            output.client_events[2],
            {
                "action_type": "FLY_TO",
                "payload": {"center": [121.5, 31.2], "zoom": 10},
            },
        )

    def test_fly_to_accepts_coordinates_alias_and_adds_default_zoom(self):
        output = execute_map_action("FLY_TO", {"coordinates": [121.5, 31.2]})

        self.assertEqual(
            [event["action_type"] for event in output.client_events],
            ["CLEAR", "MARKER_ADD", "FLY_TO"],
        )
        self.assertEqual(
            output.client_events[1]["payload"]["markers"][0]["coordinates"],
            [121.5, 31.2],
        )
        self.assertEqual(
            output.client_events[2],
            {
                "action_type": "FLY_TO",
                "payload": {"center": [121.5, 31.2], "zoom": 12},
            },
        )

    def test_clear_accepts_empty_payload(self):
        output = execute_map_action("CLEAR", {})

        self.assertEqual(output.client_events, ({"action_type": "CLEAR", "payload": {}},))

    def test_selection_clear_uses_backend_metadata_without_changing_payload(self):
        self.assertIn("clear_selection", TOOL_PARAMETERS["properties"])
        self.assertNotIn("clear_selection", TOOL_PARAMETERS["properties"]["payload"]["properties"])
        for payload in ({}, {"layer_ids": []}, {"layer_ids": ["observation-stations"]}):
            with self.subTest(payload=payload):
                before = dict(payload)
                output = execute_map_action("CLEAR", payload, clear_selection=True)
                self.assertTrue(output.client_events[0]["clear_selection"])
                self.assertEqual(output.client_events[0]["payload"], payload)
                self.assertEqual(payload, before)

    def test_selection_clear_rejects_wrong_action_type_and_frontend_parameter(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "布尔值"):
                execute_map_action("CLEAR", {}, clear_selection=value)
        with self.assertRaisesRegex(ValueError, "只能用于 CLEAR"):
            execute_map_action("FLY_TO", {"center": [118.1, 24.5]}, clear_selection=True)
        with self.assertRaisesRegex(ValueError, "不得放入前端 payload"):
            execute_map_action("CLEAR", {"clear_selection": True})

    def test_rejects_unknown_action(self):
        with self.assertRaisesRegex(ValueError, "不支持的地图动作"):
            execute_map_action("layer.upsert", {})

    def test_rejects_missing_required_payload_field(self):
        with self.assertRaisesRegex(ValueError, "center"):
            execute_map_action("FLY_TO", {"zoom": 10})

    def test_rejects_non_object_payload(self):
        with self.assertRaisesRegex(ValueError, "payload 必须为对象"):
            execute_map_action("CLEAR", [])
