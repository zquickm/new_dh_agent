import unittest

from agents.tools.spatial_query_service import TOOL_PARAMETERS, SpatialQueryServiceTool


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
        })
        self.assertFalse({"query_type", "bbox", "center", "radius", "polygon",
                          "station_types", "station_name"} & props.keys())
        requirements = {
            item["if"]["properties"]["operation"]["const"]: set(item["then"]["required"])
            for item in TOOL_PARAMETERS["allOf"]
        }
        self.assertEqual(requirements, {
            "get_spatial_element_detail": {"dataKey"}, "query_spatial_element": {"query"},
            "station_status": {"tznos"}, "observation_summary": {"tzno"},
            "observation_trend": {"tzno", "element"},
            "observation_range": {"tzno", "data_type", "start_time", "end_time"},
        })

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
