import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from aiohttp import web

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.observation_service import (
    TOOL_DESCRIPTION, TOOL_NAME, TOOL_PARAMETERS, ObservationServiceTool,
)
from tests.test_agent_loop import FakeLlm
from tool_backends.spatial_backend import SpatialServiceClient, SpatialServiceError


class SpatialElementsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.code = 0
        self.results = {
            "list": [{"dataKey": "dm_duanmian", "dataName": "断面测点"}],
            "detail": {
                "dataKey": "dm_duanmian", "maxQueryLimit": 100,
                "fields": [{"name": "code", "type": "string", "operatorWhitelist": ["="]}],
            },
            "query": {
                "type": "FeatureCollection", "total": 31, "returned": 1,
                "features": [{
                    "type": "Feature", "id": "E1001",
                    "geometry": {"type": "Point", "coordinates": [122.5, 31.3]},
                    "properties": {"code": "E1001", "longitude": 122.5, "latitude": 31.3},
                }],
            },
        }

        async def respond(request):
            payload = await request.json() if request.method == "POST" else None
            self.requests.append({
                "path": request.path, "method": request.method,
                "params": dict(request.query), "payload": payload,
                "api_key": request.headers.get("X-API-Key"),
            })
            return web.json_response({
                "code": self.code, "message": "test result",
                "data": self.results[request.path.rsplit("/", 1)[-1]],
            }, status=self.status)

        app = web.Application()
        app.router.add_get("/api/spatial/v2/elements/list", respond)
        app.router.add_get("/api/spatial/v2/elements/detail", respond)
        app.router.add_post("/api/spatial/v2/elements/query", respond)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = SpatialServiceClient(
            base_url=f"http://127.0.0.1:{port}", api_key="test-key",
            timeout=2, connect_timeout=1, verify_ssl=True,
        )
        self.tool = ObservationServiceTool(self.client)
        self.executor = ToolExecutor()
        self.executor.register(TOOL_NAME, TOOL_DESCRIPTION, self.tool.execute, TOOL_PARAMETERS)

    async def asyncTearDown(self):
        await self.tool.close()
        await self.runner.cleanup()

    async def test_discovery_detail_and_query_follow_v2_wire_contract(self):
        listing = await self.tool.execute("list_spatial_elements")
        self.assertEqual(listing.content["elements"], self.results["list"])
        detail = await self.tool.execute("get_spatial_element_detail", dataKey="dm_duanmian")
        self.assertEqual(detail.content["fields"], self.results["detail"]["fields"])
        query = {
            "element": "dm_duanmian", "mode": "attribute",
            "fields": ["code", "longitude", "latitude"],
            "conditions": [{"field": "code", "operator": "=", "value": "E1001"}],
            "conditionLogic": "AND", "includeGeometry": True, "geometryFormat": "geojson",
            "orderBy": "code", "orderDirection": "asc", "limit": 20, "offset": 0,
        }
        original = deepcopy(query)
        content, events = await self.executor.execute_with_events(TOOL_NAME, {
            "operation": "query_spatial_element", "query": query,
        })
        result = json.loads(content)
        self.assertEqual(query, original)
        self.assertEqual(self.requests[-1]["payload"], original)
        self.assertEqual(self.requests[1]["params"], {"dataKey": "dm_duanmian"})
        self.assertEqual([r["method"] for r in self.requests], ["GET", "GET", "POST"])
        self.assertTrue(all(r["api_key"] == "test-key" for r in self.requests))
        self.assertEqual(result, {**self.results["query"], "operation": "query_spatial_element"})
        self.assertEqual(events, ())

    async def test_spatial_inputs_are_forwarded_without_changing_scope(self):
        polygon = {"type": "Polygon", "coordinates": [[[122, 30], [123, 30], [123, 31], [122, 30]]]}
        spatial_inputs = [
            {"relation": "bbox", "bbox": {"west": 122, "south": 30, "east": 123, "north": 31}},
            {"relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 5000},
            {"relation": "dwithin", "geometry": polygon, "distance": 1000},
            {"relation": "intersects", "geometry": polygon, "point": {"lon": 122, "lat": 30}},
            *[{"relation": relation, "geometry": polygon}
              for relation in ("within", "contains", "touches", "overlaps", "disjoint")],
        ]
        for spatial in spatial_inputs:
            with self.subTest(relation=spatial["relation"]):
                query = {"element": "dm_duanmian", "mode": "spatial", "spatial": spatial}
                await self.tool.execute("query_spatial_element", query=query)
                self.assertEqual(self.requests[-1]["payload"], query)

    async def test_invalid_queries_fail_before_http_without_dropping_filters(self):
        invalid = [
            {}, {"element": " "}, {"element": "dm_duanmian", "mode": "spatial"},
            {"mode": "attribute", "spatial": {"relation": "bbox"}},
            {"limit": 0}, {"limit": True}, {"offset": -1}, {"geometryFormat": "wkt"},
            {"conditionLogic": "XOR"}, {"query_type": "circle"}, {"includeGeometry": "true"},
            {"conditions": [{"field": "code", "operator": "IN", "value": ["E1001"]}]},
            {"conditions": [{"field": "code", "operator": "="}]},
            {"mode": "spatial", "spatial": {"relation": "within"}},
            {"mode": "spatial", "spatial": {"relation": "dwithin", "point": {"lon": 122, "lat": 30}}},
            {"mode": "spatial", "spatial": {"relation": "dwithin", "distance": 5}},
            {"mode": "spatial", "spatial": {"relation": "dwithin", "distance": 0, "point": {"lon": 122, "lat": 30}}},
            {"mode": "spatial", "spatial": {"relation": "dwithin", "distance": 5, "point": {"lon": 181, "lat": 30}}},
            {"mode": "spatial", "spatial": {"relation": "bbox", "bbox": {"west": 123, "south": 30, "east": 122, "north": 31}}},
            {"mode": "spatial", "spatial": {"relation": "intersects", "geometry": {"type": "FeatureCollection", "features": []}}},
            {"mode": "spatial", "spatial": {"relation": "intersects", "geometry": {"type": "Point", "coordinates": [float("nan"), 30]}}},
        ]
        for update in invalid:
            query = {"element": "dm_duanmian", **update} if update else {}
            with self.subTest(query=query), self.assertRaises(ValueError):
                await self.tool.execute("query_spatial_element", query=query)
        for operation, args in [
            ("list_spatial_elements", {"station_types": ["radar"]}),
            ("get_spatial_element_detail", {}),
            ("query_spatial_element", {}),
            ("query_spatial_element", {"query": {"element": "dm_duanmian"}, "bbox": [122, 30, 123, 31]}),
        ]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                await self.tool.execute(operation, **args)
        self.assertEqual(self.requests, [])

    async def test_empty_and_nonpoint_results_preserve_geojson_and_pagination(self):
        for features in ([], [
            {"type": "Feature", "id": 1, "geometry": None, "properties": {"code": "E1001"}},
            {"type": "Feature", "id": 2, "geometry": {"type": "LineString", "coordinates": [[122, 30], [123, 31]]}, "properties": {}},
        ]):
            with self.subTest(features=features):
                self.results["query"] = {"type": "FeatureCollection", "features": features, "total": 31, "returned": len(features)}
                result = await self.tool.execute("query_spatial_element", query={"element": "dm_duanmian", "offset": 30})
                self.assertEqual(result.content["features"], features)
                self.assertEqual(result.content["total"], 31)
                self.assertEqual(result.content["returned"], len(features))
                self.assertEqual(result.client_events, ())

    async def test_service_errors_and_wrong_response_shapes_are_not_empty_successes(self):
        for status in (400, 401, 404, 503):
            self.status = status
            with self.subTest(status=status), self.assertRaises(SpatialServiceError):
                await self.tool.execute("list_spatial_elements")
        self.status = 200
        self.code = 400
        with self.assertRaises(SpatialServiceError):
            await self.tool.execute("list_spatial_elements")
        self.code = 0
        self.results["list"] = {}
        with self.assertRaises(SpatialServiceError):
            await self.tool.execute("list_spatial_elements")
        self.results["detail"] = []
        with self.assertRaises(SpatialServiceError):
            await self.tool.execute("get_spatial_element_detail", dataKey="dm_duanmian")
        self.results["query"] = {"stations": []}
        with self.assertRaises(SpatialServiceError):
            await self.tool.execute("query_spatial_element", query={"element": "dm_duanmian"})

    async def test_existing_agent_loop_completes_discovery_and_query_in_five_rounds(self):
        self.executor.register("load_skill", "加载能力", lambda: "使用通用空间操作查询断面位置。")
        calls = [
            ("load_skill", {}),
            (TOOL_NAME, {"operation": "list_spatial_elements"}),
            (TOOL_NAME, {"operation": "get_spatial_element_detail", "dataKey": "dm_duanmian"}),
            (TOOL_NAME, {"operation": "query_spatial_element", "query": {"element": "dm_duanmian"}}),
        ]
        llm = FakeLlm([
            *[([], [{"id": f"call-{i}", "name": name, "args": args}]) for i, (name, args) in enumerate(calls)],
            (["已找到断面测点 E1001。"], []),
        ])
        agent = AgentLoop(llm, self.executor)
        agent.max_rounds = 5
        with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
            events = [event async for event in agent.run_stream("查询断面测点位置", [])]
        finished = [event for event in events if event.type == "tool_finished"]
        self.assertEqual(len(finished), 4)
        self.assertTrue(all(event.success for event in finished))
        self.assertIn("E1001", finished[-1].content)
        self.assertEqual(len(llm.calls), 5)
        self.assertFalse(any(event.type == "map_action" for event in events))
