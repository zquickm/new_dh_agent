import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

from agents.tools.map_action import execute_map_action, spatial_map_results
from agents.tools.spatial_query_service import TOOL_PARAMETERS, SpatialQueryServiceTool
from tool_backends.spatial_backend import SpatialServiceError


class FakeSpatialClient:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def query_station_status(self, tznos):
        self.calls.append(("query_station_status", tznos))
        return {"statusList": [], "onlineCount": 0, "offlineCount": 0}

    async def get_observation_summary(self, tzno):
        self.calls.append(("get_observation_summary", tzno))
        return {"tzno": tzno, "statusRecord": {"watertemp": 25.3}}

    async def get_element_trend(self, tzno, element, hours):
        self.calls.append(("get_element_trend", tzno, element, hours))
        return {"tzno": tzno, "element": element, "points": []}

    async def get_observation_range(self, **arguments):
        self.calls.append(("get_observation_range", arguments))
        return {"tzno": arguments["tzno"], "records": []}

    async def close(self):
        self.closed = True


class SpatialQueryServiceToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_auto_pagination_uses_actual_returned_count_without_mutating_query(self):
        features = [{"type": "Feature", "id": index, "geometry": None, "properties": {"name": str(index)}} for index in range(3)]
        client = FakeSpatialClient()
        client.query_spatial_element = AsyncMock(side_effect=[
            {"type": "FeatureCollection", "total": 3, "returned": 1, "features": [feature]} for feature in features
        ])
        query = {"element": "partner_island", "limit": 20, "includeGeometry": True,
                 "conditions": [{"field": "name", "operator": "LIKE", "value": "%岛%"}]}
        original = deepcopy(query)
        output = await SpatialQueryServiceTool(client).execute("query_spatial_element", query=query)
        self.assertEqual(query, original)
        self.assertEqual(output.content["features"], features)
        self.assertTrue(output.content["pagination"]["complete"])
        self.assertEqual([call.args[0] for call in client.query_spatial_element.call_args_list], [
            original, {**original, "offset": 1}, {**original, "offset": 2},
        ])

    async def test_pagination_failures_keep_partial_data_and_never_claim_complete(self):
        feature = {"type": "Feature", "id": 1, "geometry": None, "properties": {"name": "海岛"}}
        first = {"type": "FeatureCollection", "total": 3, "returned": 1, "features": [feature]}
        cases = [
            (deepcopy(first), "重复"),
            ({**first, "features": [], "returned": 0}, "空页"),
            ({**first, "total": 4}, "total"),
            ({**first, "returned": 2}, "returned"),
            (SpatialServiceError("后续页 HTTP 500"), "HTTP 500"),
        ]
        for second, expected in cases:
            with self.subTest(expected=expected):
                client = FakeSpatialClient()
                client.query_spatial_element = AsyncMock(side_effect=[deepcopy(first), second])
                token = spatial_map_results.set({})
                try:
                    output = await SpatialQueryServiceTool(client).execute(
                        "query_spatial_element", query={"element": "partner_island", "includeGeometry": True},
                    )
                    action = execute_map_action("GEOJSON_ADD", {"result_ref": output.content["result_ref"]})
                finally:
                    spatial_map_results.reset(token)
                self.assertEqual(output.content["features"], [feature])
                self.assertEqual(output.content["total"], 3)
                self.assertEqual(output.content["returned"], 1)
                pagination = output.content["pagination"]
                self.assertFalse(pagination["complete"])
                self.assertEqual(pagination["next_offset"], 1)
                self.assertIn(expected, pagination["warning"])
                self.assertEqual(action.client_events[0]["payload"]["geojson"]["features"], [feature])
                self.assertEqual(client.query_spatial_element.await_count, 2)

    async def test_empty_results_and_nonzero_offsets_are_reported_accurately(self):
        client = FakeSpatialClient()
        client.query_spatial_element = AsyncMock(return_value={"type": "FeatureCollection", "features": [], "total": 0, "returned": 0})
        tool = SpatialQueryServiceTool(client)
        output = await tool.execute("query_spatial_element", query={"element": "partner_island"})
        self.assertTrue(output.content["pagination"]["complete"])
        self.assertEqual(output.content["returned"], 0)
        client.query_spatial_element.return_value = {
            "type": "FeatureCollection", "total": 3, "returned": 1,
            "features": [{"type": "Feature", "id": 3, "geometry": None, "properties": {}}],
        }
        output = await tool.execute("query_spatial_element", query={"element": "partner_island", "offset": 2})
        self.assertFalse(output.content["pagination"]["complete"])
        self.assertIn("未包含此前记录", output.content["pagination"]["warning"])
        self.assertIsNone(output.content["pagination"]["next_offset"])

    async def test_fetch_all_validation_and_invalid_first_page_fail_without_empty_success(self):
        client = FakeSpatialClient()
        client.query_spatial_element = AsyncMock(return_value={"type": "FeatureCollection", "features": []})
        tool = SpatialQueryServiceTool(client)
        for value in (None, "true", 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "fetch_all"):
                await tool.execute("query_spatial_element", query={"element": "partner_island"}, fetch_all=value)
        client.query_spatial_element.assert_not_awaited()
        with self.assertRaisesRegex(ValueError, "total"):
            await tool.execute("query_spatial_element", query={"element": "partner_island"})
        with self.assertRaisesRegex(ValueError, "仅接受"):
            await tool.execute("list_spatial_elements", fetch_all=False)

    async def test_island_pagination_keeps_partial_statistics_on_later_page_failure(self):
        first = {"total": 3, "items": [{"id": 1}, {"id": 2}], "page": 1, "size": 2,
                 "byProvince": [{"province": "浙江省", "count": 3}]}
        client = FakeSpatialClient()
        client.get_island_overview = AsyncMock(side_effect=[first, SpatialServiceError("海岛后续页查询失败")])
        result = await SpatialQueryServiceTool(client).execute("island_overview", province="浙江省", size=2)
        self.assertEqual(result.content["items"], first["items"])
        self.assertEqual(result.content["byProvince"], first["byProvince"])
        self.assertFalse(result.content["pagination"]["complete"])
        self.assertEqual(result.content["pagination"]["next_page"], 2)
        self.assertIn("后续页查询失败", result.content["pagination"]["warning"])
        self.assertEqual(client.get_island_overview.call_args.kwargs, {"province": "浙江省", "size": 2, "page": 2})

    async def test_all_station_operations_only_call_the_requested_data_endpoint(self):
        client = FakeSpatialClient()
        tool = SpatialQueryServiceTool(client)
        cases = [
            ("station_status", {"tznos": ["A001"]}, "query_station_status"),
            ("station_status", {"tznos": ["A001", "A002"]}, "query_station_status"),
            ("observation_summary", {"tzno": "A001"}, "get_observation_summary"),
            ("observation_trend", {"tzno": "A001", "element": "watertemp"}, "get_element_trend"),
            ("observation_range", {"tzno": "A001", "data_type": "watertemp",
                "start_time": "2026-08-23 00:00:00", "end_time": "2026-08-24 00:00:00"}, "get_observation_range"),
        ]
        for operation, arguments, endpoint in cases:
            with self.subTest(operation=operation, arguments=arguments):
                client.calls.clear()
                output = await tool.execute(operation, **arguments)
                self.assertEqual(output.client_events, ())
                self.assertEqual(output.content["operation"], operation)
                self.assertEqual([call[0] for call in client.calls], [endpoint])

    async def test_removed_render_map_is_not_silently_accepted(self):
        self.assertNotIn("render_map", TOOL_PARAMETERS["properties"])
        client = FakeSpatialClient()
        with self.assertRaisesRegex(ValueError, "map_action"):
            await SpatialQueryServiceTool(client).execute("query_spatial_element", render_map=True)
        self.assertEqual(client.calls, [])

    async def test_range_query_validates_and_normalizes_parameters(self):
        client = FakeSpatialClient()
        tool = SpatialQueryServiceTool(client)

        output = await tool.execute(
            "observation_range",
            tzno="A001",
            data_type="watertemp",
            start_time="2026-08-23 00:00:00",
            end_time="2026-08-24 00:00:00",
            order_by="DESC",
            max_records=5000,
        )

        self.assertEqual(output.content["operation"], "observation_range")
        call = client.calls[0][1]
        self.assertEqual(call["order_by"], "desc")
        self.assertEqual(call["max_records"], 5000)

    async def test_close_closes_injected_client(self):
        client = FakeSpatialClient()
        tool = SpatialQueryServiceTool(client)

        await tool.close()

        self.assertTrue(client.closed)

    def test_schema_only_exposes_generic_spatial_and_observation_data_operations(self):
        props = TOOL_PARAMETERS["properties"]
        self.assertEqual(set(props["operation"]["enum"]), {
            "list_spatial_elements", "get_spatial_element_detail", "query_spatial_element",
            "station_status", "observation_summary", "observation_trend", "observation_range",
            "island_overview", "dm_duanmian_overview",
            "dm_duanmian_latest", "dm_duanmian_observations", "dm_duanmian_voyages",
        })
        self.assertFalse({"query_type", "bbox", "center", "radius", "polygon",
                          "station_types", "station_name"} & props.keys())
        requirements = {
            item["if"]["properties"]["operation"]["const"]: set(item["then"]["required"])
            for item in TOOL_PARAMETERS["allOf"]
            if "required" in item["then"]
        }
        self.assertEqual(requirements, {
            "get_spatial_element_detail": {"dataKey"}, "query_spatial_element": {"query"},
            "station_status": {"tznos"}, "observation_summary": {"tzno"},
            "observation_trend": {"tzno", "element"},
            "observation_range": {"tzno", "data_type", "start_time", "end_time"},
            "dm_duanmian_latest": {"code"}, "dm_duanmian_voyages": {"code"},
            "dm_duanmian_observations": {"from", "to"},
        })

    async def test_invalid_transect_arguments_fail_before_http(self):
        client = FakeSpatialClient()
        client.get_duanmian_latest = AsyncMock()
        client.get_duanmian_voyages = AsyncMock()
        client.get_duanmian_observations = AsyncMock()
        tool = SpatialQueryServiceTool(client)
        dates = {"from": "2025-08-01", "to": "2025-11-01"}
        cases = [
            *[(operation, arguments) for operation in ("dm_duanmian_latest", "dm_duanmian_voyages")
              for arguments in ({}, {"code": " "}, {"code": "E1", "page": 1})],
            ("dm_duanmian_observations", {}),
            ("dm_duanmian_observations", {"from": "2025-08-01"}),
            ("dm_duanmian_observations", {"to": "2025-11-01"}),
            *[("dm_duanmian_observations", {**dates, **update}) for update in (
                {"from": "2025-02-30"}, {"from": "2025-8-01"}, {"to": "invalid"},
                {"from": "2025-12-01"}, {"code": " "}, {"code": 123},
                {"page": 0}, {"page": True}, {"page": 1.5},
                {"size": 0}, {"size": 1001}, {"size": "100"},
                {"fetch_all": True},
            )],
        ]
        for operation, arguments in cases:
            with self.subTest(operation=operation, arguments=arguments), self.assertRaises(ValueError):
                await tool.execute(operation, **arguments)
        client.get_duanmian_latest.assert_not_awaited()
        client.get_duanmian_voyages.assert_not_awaited()
        client.get_duanmian_observations.assert_not_awaited()

    async def test_removed_operations_and_extra_parameters_fail_before_http(self):
        client = FakeSpatialClient()
        tool = SpatialQueryServiceTool(client)
        for operation in ("list_station_types", "search_stations", "station_detail"):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "不支持"):
                await tool.execute(operation)
        for operation in TOOL_PARAMETERS["properties"]["operation"]["enum"]:
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "仅接受参数"):
                await tool.execute(operation, station_types=["radar"])
        self.assertEqual(client.calls, [])

    async def test_invalid_observation_arguments_fail_before_http(self):
        client = FakeSpatialClient()
        tool = SpatialQueryServiceTool(client)
        time_range = {"tzno": "A001", "data_type": "watertemp",
                      "start_time": "2026-08-23 00:00:00", "end_time": "2026-08-24 00:00:00"}
        cases = [
            ("station_status", {"tznos": []}),
            ("station_status", {"tznos": ["A001"] * 51}),
            ("observation_summary", {"tzno": " "}),
            ("observation_trend", {"tzno": "A001", "element": "watertemp", "hours": 0}),
            *[("observation_range", {**time_range, **update}) for update in [
                {"data_type": "unknown"}, {"start_time": "invalid"},
                {"start_time": "2026-08-25 00:00:00"}, {"order_by": "invalid"},
                {"max_records": 5001},
            ]],
        ]
        for operation, arguments in cases:
            with self.subTest(operation=operation, arguments=arguments), self.assertRaises(ValueError):
                await tool.execute(operation, **arguments)
        self.assertEqual(client.calls, [])
