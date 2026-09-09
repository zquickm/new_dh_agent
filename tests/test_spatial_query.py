import json
import unittest
from copy import deepcopy

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.map_action import execute_map_action
from agents.tools.spatial_query import SpatialQueryTool
from controller.streaming.sse_encoder import encode_chat_stream
from schemas.chat import AgentRequest
from services.chat_service import ChatService
from services.request_context_adapter import enrich_question_with_frontend_context
from tool_backends.spatial_backend import SpatialServiceError


DETAIL = {
    "dataKey": "dm_duanmian",
    "dataName": "断面测点",
    "srid": 4490,
    "geometryType": "Point",
    "idColumn": "code",
    "table": "private.table",
    "defaultDisplayFields": ["code"],
    "maxQueryLimit": 100,
    "fields": [
        {"name": "code", "type": "string", "operatorWhitelist": ["=", "LIKE"]},
        {
            "name": "status",
            "type": "string",
            "operatorWhitelist": ["="],
            "enums": ["active"],
        },
        {"name": "depth", "type": "number", "operatorWhitelist": [">="]},
    ],
}
FEATURE = {
    "type": "Feature",
    "id": "E1001",
    "geometry": {"type": "Point", "coordinates": [122.503333, 31.333333]},
    "properties": {"code": "E1001"},
}
AREA = {
    "query_type": "circle",
    "center": [122.5, 31.3],
    "radius": 25000,
    "crs": "EPSG:4490",
}


class FakeSpatialClient:
    def __init__(self):
        self.detail = deepcopy(DETAIL)
        self.data = {
            "type": "FeatureCollection",
            "features": [deepcopy(FEATURE)],
            "total": 1,
            "returned": 1,
        }
        self.calls = []
        self.closed = False

    async def list_spatial_elements(self):
        return [
            {"dataKey": "dm_duanmian", "dataName": "断面测点", "geometryType": "Point"}
        ]

    async def get_spatial_element_detail(self, element):
        self.calls.append(("detail", element))
        return deepcopy(self.detail)

    async def query_spatial_element(self, payload):
        self.calls.append(("query", payload))
        return deepcopy(self.data)

    async def close(self):
        self.closed = True


class SpatialQueryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeSpatialClient()
        self.tool = SpatialQueryTool(self.client)

    async def asyncTearDown(self):
        await self.tool.close()

    def test_single_element_follow_up_keeps_its_object_instead_of_becoming_a_station(
        self,
    ):
        request = AgentRequest.model_validate(
            {
                "question": "它的位置在哪里",
                "map_context": {
                    "drawn_geometry": {
                        "center": AREA["center"],
                        "radius": AREA["radius"],
                    },
                },
            }
        )
        question = enrich_question_with_frontend_context(
            request.question, request.context
        )
        self.assertIn("继承其 element 和真实标识", question)
        self.assertIn("不得改成业务观测站", question)
        self.assertNotIn("selected_area", question)

    async def test_discovery_has_no_map_events_and_detail_omits_physical_table(self):
        catalog = await self.tool.execute(operation="list")
        detail = await self.tool.execute(operation="detail", element="dm_duanmian")
        self.assertEqual(catalog.content["elements"][0]["dataKey"], "dm_duanmian")
        self.assertEqual(detail.content["fields"], DETAIL["fields"])
        self.assertNotIn("table", detail.content)
        self.assertEqual(catalog.client_events, ())
        self.assertEqual(detail.client_events, ())

    async def test_circle_queries_real_geometry_and_emits_existing_marker_contract(
        self,
    ):
        output = await self.tool.execute(
            operation="query", element="dm_duanmian", area=AREA
        )
        payload = self.client.calls[-1][1]
        self.assertEqual(
            payload["spatial"],
            {
                "relation": "dwithin",
                "point": {"lon": 122.5, "lat": 31.3},
                "distance": 25000,
            },
        )
        self.assertEqual(payload["fields"], ["code"])
        self.assertEqual(payload["orderBy"], "code")
        self.assertTrue(payload["includeGeometry"])
        self.assertEqual(
            [e["action_type"] for e in output.client_events], ["CLEAR", "MARKER_ADD"]
        )
        self.assertEqual(
            output.client_events[0]["payload"], {"layer_ids": ["spatial-dm_duanmian"]}
        )
        marker = output.client_events[1]["payload"]["markers"][0]
        self.assertEqual(
            marker,
            {
                "id": "dm_duanmian:E1001",
                "name": "E1001",
                "type": "dm_duanmian",
                "coordinates": [122.503333, 31.333333],
            },
        )
        self.assertTrue(output.client_events[1]["payload"]["fit_bounds"])
        self.assertEqual(output.content["map_count"], 1)

    async def test_polygon_preserves_concave_boundary_instead_of_using_bbox(self):
        ring = [[121, 30], [123, 30], [122, 31], [123, 32], [121, 32], [121, 30]]
        area = {"query_type": "polygon", "polygon": ring, "bbox": [121, 30, 123, 32]}
        original = deepcopy(area)
        await self.tool.execute(operation="query", element="dm_duanmian", area=area)
        self.assertEqual(
            self.client.calls[-1][1]["spatial"],
            {
                "relation": "intersects",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            },
        )
        self.assertEqual(area, original)

    async def test_bbox_and_attribute_filter_are_combined_without_changing_values(self):
        await self.tool.execute(
            operation="query",
            element="dm_duanmian",
            area={"query_type": "bbox", "bbox": [121, 30, 123, 32]},
            conditions=[{"field": "code", "operator": "LIKE", "value": "E1%"}],
        )
        payload = self.client.calls[-1][1]
        self.assertEqual(
            payload["spatial"],
            {
                "relation": "bbox",
                "bbox": {"west": 121, "south": 30, "east": 123, "north": 32},
            },
        )
        self.assertEqual(
            payload["conditions"],
            [{"field": "code", "operator": "LIKE", "value": "E1%"}],
        )
        self.assertEqual(payload["conditionLogic"], "AND")

    async def test_attribute_only_lookup_and_pagination_can_avoid_map_updates(self):
        self.client.data.update(total=55)
        output = await self.tool.execute(
            operation="query",
            element="dm_duanmian",
            render_map=False,
            offset=20,
            conditions=[{"field": "code", "operator": "=", "value": "E1001"}],
        )
        payload = self.client.calls[-1][1]
        self.assertEqual(payload["mode"], "attribute")
        self.assertNotIn("spatial", payload)
        self.assertFalse(payload["includeGeometry"])
        self.assertEqual(output.client_events, ())
        self.assertEqual((output.content["total"], output.content["returned"]), (55, 1))
        self.assertTrue(output.content["has_more"])
        self.assertTrue(output.content["partial"])

    async def test_empty_query_clears_only_its_own_layer(self):
        self.client.data.update(features=[], total=0, returned=0)
        output = await self.tool.execute(
            operation="query", element="dm_duanmian", area=AREA
        )
        self.assertEqual(
            output.client_events,
            (
                {
                    "action_type": "CLEAR",
                    "payload": {"layer_ids": ["spatial-dm_duanmian"]},
                },
            ),
        )
        self.assertEqual(output.content["map_count"], 0)
        self.assertFalse(output.content["has_more"])

    async def test_unmappable_and_duplicate_features_do_not_fabricate_markers_or_totals(
        self,
    ):
        features = [deepcopy(FEATURE), deepcopy(FEATURE)]
        features[0]["properties"]["type"] = "transect-point"
        for geometry in [
            None,
            {"type": "Point", "coordinates": [float("nan"), 31]},
            {"type": "Point", "coordinates": [True, 31]},
            {"type": "Point", "coordinates": [181, 31]},
            {
                "type": "Polygon",
                "coordinates": [[[121, 30], [122, 30], [122, 31], [121, 30]]],
            },
        ]:
            features.append(
                {
                    **deepcopy(FEATURE),
                    "id": f"other-{len(features)}",
                    "geometry": geometry,
                }
            )
        self.client.data.update(features=features, returned=len(features), total=26)
        output = await self.tool.execute(
            operation="query", element="dm_duanmian", area=AREA
        )
        self.assertEqual(output.content["total"], 26)
        self.assertEqual(output.content["returned"], len(features))
        self.assertEqual(output.content["map_count"], 1)
        self.assertEqual(output.content["unmapped_count"], len(features) - 1)
        self.assertEqual(
            output.client_events[1]["payload"]["markers"][0]["type"], "transect-point"
        )

    async def test_invalid_area_and_unknown_arguments_are_rejected_before_network(self):
        invalid = [
            {"area": {"query_type": "circle", "center": [True, 31], "radius": 1}},
            {
                "area": {
                    "query_type": "circle",
                    "center": [122, 31],
                    "radius": float("inf"),
                }
            },
            {"area": {"query_type": "circle", "center": [122, 31]}},
            {"area": {"query_type": "bbox", "bbox": [123, 30, 121, 32]}},
            {
                "area": {
                    "query_type": "polygon",
                    "polygon": [[121, 30], [122, 31], [123, 32]],
                }
            },
            {"area": {**AREA, "crs": "EPSG:4326"}},
            {"limit": True},
            {"offset": -1},
            {"render_map": "false"},
            {"mode": "spatial"},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.tool.execute(
                    operation="query", element="dm_duanmian", **arguments
                )
        self.assertEqual(self.client.calls, [])

    async def test_metadata_limits_fields_operators_enums_and_types_before_query(self):
        invalid = [
            {"fields": ["secret"]},
            {"order_by": "secret"},
            {"limit": 101},
            {"conditions": [{"field": "unknown", "operator": "=", "value": "x"}]},
            {"conditions": [{"field": "code", "operator": ">", "value": "x"}]},
            {"conditions": [{"field": "status", "operator": "=", "value": "unknown"}]},
            {"conditions": [{"field": "depth", "operator": ">=", "value": "100"}]},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.tool.execute(
                    operation="query", element="dm_duanmian", **arguments
                )
        self.assertFalse(any(call[0] == "query" for call in self.client.calls))

    async def test_bad_upstream_response_is_not_treated_as_empty_result(self):
        for update in [
            {"total": -1},
            {"features": None},
            {"returned": 0},
            {"type": "Feature"},
        ]:
            self.client.data = {
                "type": "FeatureCollection",
                "features": [deepcopy(FEATURE)],
                "total": 1,
                "returned": 1,
                **update,
            }
            with self.subTest(update=update), self.assertRaises(SpatialServiceError):
                await self.tool.execute(
                    operation="query", element="dm_duanmian", area=AREA
                )

    async def test_query_flows_through_chat_sse_without_a_second_map_tool_call(self):
        class FakeLlm:
            model = "fake-model"
            calls = 0

            async def stream(inner, messages, tools=None, temperature=0):
                inner.calls += 1
                if inner.calls == 1:
                    self.assertIn('"query_type":"circle"', messages[-1]["content"])
                    yield (
                        "",
                        [
                            {
                                "id": "spatial-call",
                                "name": "spatial_query",
                                "args": {
                                    "operation": "query",
                                    "element": "dm_duanmian",
                                    "area": AREA,
                                },
                            }
                        ],
                    )
                else:
                    yield "查询到 1 个断面测点。", []

        executor = ToolExecutor()
        executor.register("spatial_query", "空间要素查询", self.tool.execute)
        service = ChatService()
        service._agent = AgentLoop(FakeLlm(), executor)
        request = AgentRequest.model_validate(
            {
                "question": "这个区域有哪些断面测点",
                "map_context": {
                    "drawn_geometry": {
                        "center": AREA["center"],
                        "radius": AREA["radius"],
                    },
                },
            }
        )
        try:
            frames = [
                frame
                async for frame in encode_chat_stream(
                    service.stream_chat(request, user_id="spatial-test")
                )
            ]
        finally:
            await service.close()
        actions = [
            json.loads(frame.split("\ndata: ", 1)[1])["data"]
            for frame in frames
            if frame.startswith("event: map_action\n")
        ]
        self.assertEqual([a["action_type"] for a in actions], ["CLEAR", "MARKER_ADD"])
        self.assertEqual([a["seq"] for a in actions], [1, 2])
        self.assertEqual(
            [a["action_id"] for a in actions], ["spatial-call-1", "spatial-call-2"]
        )
        self.assertEqual(actions[0]["run_id"], actions[1]["run_id"])
        self.assertEqual(actions[0]["version"], "1.0")
        self.assertEqual(actions[0]["batch_id"], "spatial-call")
        self.assertTrue(actions[0]["conversation_id"])
        self.assertTrue(frames[-1].startswith("event: done\n"))

    async def test_auto_spatial_map_prevents_a_later_duplicate_fly_to(self):
        class FakeLlm:
            model = "fake-model"
            calls = 0

            async def stream(inner, messages, tools=None, temperature=0):
                inner.calls += 1
                if inner.calls == 1:
                    yield (
                        "",
                        [
                            {
                                "id": "query",
                                "name": "spatial_query",
                                "args": {
                                    "operation": "query",
                                    "element": "dm_duanmian",
                                    "area": AREA,
                                },
                            }
                        ],
                    )
                elif inner.calls == 2:
                    yield (
                        "",
                        [
                            {
                                "id": "duplicate",
                                "name": "map_action",
                                "args": {
                                    "action_type": "FLY_TO",
                                    "payload": {"center": [122, 31]},
                                },
                            }
                        ],
                    )
                else:
                    yield "查询完成", []

        executor = ToolExecutor()
        executor.register("spatial_query", "空间查询", self.tool.execute)
        executor.register("map_action", "地图操作", execute_map_action)
        agent = AgentLoop(FakeLlm(), executor)
        events = [event async for event in agent.run_stream("圈选断面测点", [])]
        self.assertEqual(
            [
                event.data["action_type"]
                for event in events
                if event.type == "map_action"
            ],
            ["CLEAR", "MARKER_ADD"],
        )

    async def test_service_failure_reaches_model_for_recovery_without_map_events(self):
        async def unavailable(_element):
            raise SpatialServiceError("空间服务返回 HTTP 503")

        self.client.get_spatial_element_detail = unavailable

        class FakeLlm:
            model = "fake-model"
            calls = 0

            async def stream(inner, messages, tools=None, temperature=0):
                inner.calls += 1
                if inner.calls == 1:
                    yield (
                        "",
                        [
                            {
                                "id": "query",
                                "name": "spatial_query",
                                "args": {
                                    "operation": "query",
                                    "element": "dm_duanmian",
                                    "area": AREA,
                                },
                            }
                        ],
                    )
                else:
                    self.assertIn("HTTP 503", messages[-1]["content"])
                    yield "空间查询服务暂时不可用。", []

        executor = ToolExecutor()
        executor.register("spatial_query", "空间查询", self.tool.execute)
        agent = AgentLoop(FakeLlm(), executor)
        events = [event async for event in agent.run_stream("圈选断面测点", [])]
        self.assertFalse(any(event.type == "map_action" for event in events))
        self.assertFalse(
            next(event for event in events if event.type == "tool_finished").success
        )
