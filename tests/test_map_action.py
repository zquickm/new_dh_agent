import unittest

from agents.tools.map_action import TOOL_PARAMETERS, execute_map_action


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

    def test_fly_to_clears_old_layers_adds_geojson_and_moves_the_view(self):
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
            ["CLEAR", "GEOJSON_ADD", "FLY_TO"],
        )
        self.assertEqual(
            output.client_events[0]["payload"]["layer_ids"],
            ["observation-stations", "map-location-markers"],
        )
        self.assertEqual(
            output.client_events[1]["payload"]["geojson"]["features"],
            [{
                "type": "Feature",
                "id": "fly-to-target",
                "properties": {"name": "目标站点"},
                "geometry": {"type": "Point", "coordinates": [121.5, 31.2]},
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
            ["CLEAR", "GEOJSON_ADD", "FLY_TO"],
        )
        self.assertEqual(
            output.client_events[1]["payload"]["geojson"]["features"][0]["geometry"]["coordinates"],
            [121.5, 31.2],
        )
        self.assertEqual(
            output.client_events[2],
            {
                "action_type": "FLY_TO",
                "payload": {"center": [121.5, 31.2], "zoom": 12},
            },
        )

    def test_station_flight_preserves_details_and_derives_center(self):
        geojson = {"type": "FeatureCollection", "features": [{
            "type": "Feature", "id": "A001",
            "geometry": {"type": "Point", "coordinates": [121.5, 31.2]},
            "properties": {"tzno": "A001", "name": "测试站", "type": "潮位站",
                           "region": "测试区域", "water_level": 0, "status": None},
        }]}
        payload = {"geojson": geojson}
        output = execute_map_action("FLY_TO", payload)
        self.assertEqual([e["action_type"] for e in output.client_events],
                         ["CLEAR", "GEOJSON_ADD", "FLY_TO"])
        self.assertEqual(output.client_events[1]["payload"], {
            "layer_id": "map-location-markers", "geojson": geojson,
            "replace": True, "fit_bounds": False,
        })
        self.assertEqual(output.client_events[2]["payload"], {"center": [121.5, 31.2], "zoom": 12})
        self.assertEqual(payload, {"geojson": geojson})
        for invalid in (
            {"geojson": None},
            {"geojson": {"type": "FeatureCollection", "features": []}},
            {"geojson": {**geojson, "features": geojson["features"] * 2}},
            {"center": [120, 30]},
            {"geojson": {"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {}, "geometry": None,
            }]}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                execute_map_action("FLY_TO", {**payload, **invalid})

    def test_flight_rejects_invalid_coordinates_and_zoom(self):
        for invalid in ([181, 30], [120, 91], [True, 30], [float("nan"), 30], [120, float("inf")]):
            with self.subTest(center=invalid), self.assertRaises(ValueError):
                execute_map_action("FLY_TO", {"center": invalid})
        with self.assertRaises(ValueError):
            execute_map_action("FLY_TO", {"center": [120, 30], "zoom": float("nan")})

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
