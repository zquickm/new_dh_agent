import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from agents.agent_loop import AgentLoop
from agents.skills import SkillCatalog
from agents.tools.executor import ToolExecutor
from agents.tools.load_skill import load_skill
from agents.tools.observation_service import (
    TOOL_DESCRIPTION, TOOL_NAME, TOOL_PARAMETERS, ObservationServiceTool,
)
from tests.test_agent_loop import FakeLlm
from schemas.chat import FrontendContext
from tool_backends.spatial_backend import SpatialServiceClient, SpatialServiceError


class SpatialElementsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.code = 0
        self.element_results = {}
        self.results = {
            "list": [{"dataKey": "test_point", "dataName": "测试点"}],
            "detail": {
                "dataKey": "test_point", "maxQueryLimit": 100,
                "idColumn": "code",
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
            data = self.results[request.path.rsplit("/", 1)[-1]]
            if payload is not None and self.element_results:
                data = self.element_results[payload["element"]]
                start = payload.get("offset", 0)
                features = data["features"][start:start + payload.get("limit", 20)]
                data = {**data, "features": features, "returned": len(features)}
            return web.json_response({
                "code": self.code, "message": "test result",
                "data": data,
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
        detail = await self.tool.execute("get_spatial_element_detail", dataKey="test_point")
        self.assertEqual(detail.content["fields"], self.results["detail"]["fields"])
        query = {
            "element": "test_point", "mode": "attribute",
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
        self.assertEqual(self.requests[1]["params"], {"dataKey": "test_point"})
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
                query = {"element": "test_point", "mode": "spatial", "spatial": spatial}
                await self.tool.execute("query_spatial_element", query=query, render_map=False)
                self.assertEqual(self.requests[-1]["payload"], query)

    async def test_invalid_queries_fail_before_http_without_dropping_filters(self):
        invalid = [
            {}, {"element": " "}, {"element": "test_point", "mode": "spatial"},
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
            query = {"element": "test_point", **update} if update else {}
            with self.subTest(query=query), self.assertRaises(ValueError):
                await self.tool.execute("query_spatial_element", query=query)
        for operation, args in [
            ("list_spatial_elements", {"station_types": ["radar"]}),
            ("get_spatial_element_detail", {}),
            ("query_spatial_element", {}),
            ("query_spatial_element", {"query": {"element": "test_point"}, "bbox": [122, 30, 123, 31]}),
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
                result = await self.tool.execute("query_spatial_element", query={"element": "test_point", "offset": 30})
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
            await self.tool.execute("get_spatial_element_detail", dataKey="test_point")
        self.results["query"] = {"stations": []}
        with self.assertRaises(SpatialServiceError):
            await self.tool.execute("query_spatial_element", query={"element": "test_point"})

    async def test_existing_agent_loop_completes_discovery_and_query_in_five_rounds(self):
        self.executor.register("load_skill", "加载能力", lambda: "使用通用空间操作查询测试点位置。")
        calls = [
            ("load_skill", {}),
            (TOOL_NAME, {"operation": "list_spatial_elements"}),
            (TOOL_NAME, {"operation": "get_spatial_element_detail", "dataKey": "test_point"}),
            (TOOL_NAME, {"operation": "query_spatial_element", "query": {"element": "test_point"}}),
        ]
        llm = FakeLlm([
            *[([], [{"id": f"call-{i}", "name": name, "args": args}]) for i, (name, args) in enumerate(calls)],
            (["已找到测试点 E1001。"], []),
        ])
        agent = AgentLoop(llm, self.executor)
        agent.max_rounds = 5
        with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
            events = [event async for event in agent.run_stream("查询测试点位置", [])]
        finished = [event for event in events if event.type == "tool_finished"]
        self.assertEqual(len(finished), 4)
        self.assertTrue(all(event.success for event in finished))
        self.assertIn("E1001", finished[-1].content)
        self.assertEqual(len(llm.calls), 5)
        self.assertFalse(any(event.type == "map_action" for event in events))

    async def test_selected_area_queries_all_catalog_elements_in_one_round(self):
        catalog = SkillCatalog(Path(__file__).resolve().parents[1] / "skills")
        self.executor.register("load_skill", "加载能力", lambda name: load_skill(catalog, name))
        self.results["list"] = [
            {"dataKey": "obs_station", "dataName": "观测站点"},
            {"dataKey": "test_point", "dataName": "测试点"},
            {"dataKey": "test_area", "dataName": "测试面要素"},
        ]
        self.element_results = {
            "obs_station": {"type": "FeatureCollection", "total": 0, "returned": 0, "features": []},
            "test_point": {**self.results["query"], "total": 1},
            "test_area": {
                "type": "FeatureCollection", "total": 1, "returned": 1,
                "features": [{"type": "Feature", "id": 1, "geometry": None, "properties": {"name": "区域甲"}}],
            },
        }
        cases = [
            (
                {"type": "Circle", "center": [122.1, 30.2], "radius": 5000},
                {"relation": "dwithin", "point": {"lon": 122.1, "lat": 30.2}, "distance": 5000.0},
            ),
            (
                {"type": "Polygon", "coordinates": [[[122, 30], [123, 30], [122, 31], [122, 30]]]},
                {"relation": "intersects", "geometry": {"type": "Polygon", "coordinates": [[[122.0, 30.0], [123.0, 30.0], [122.0, 31.0], [122.0, 30.0]]]}},
            ),
        ]
        for geometry, spatial in cases:
            with self.subTest(geometry=geometry["type"]):
                self.requests.clear()
                query_calls = [{
                    "id": item["dataKey"], "name": TOOL_NAME,
                    "args": {"operation": "query_spatial_element", "query": {
                        "element": item["dataKey"], "mode": "spatial", "spatial": spatial,
                    }},
                } for item in self.results["list"]]
                llm = FakeLlm([
                    ([], [{"id": "skill", "name": "load_skill", "args": {"name": "空间联动"}}]),
                    ([], [{"id": "list", "name": TOOL_NAME, "args": {"operation": "list_spatial_elements"}}]),
                    ([], query_calls),
                    (["测试点1个；测试面要素1个缺少几何；观测站点0个。"], []),
                ])
                agent = AgentLoop(llm, self.executor, skills=catalog)
                agent.max_rounds = 5
                with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
                    events = [event async for event in agent.run_stream(
                        "有哪些", [], frontend_context=FrontendContext.model_validate({"drawn_geometry": geometry}),
                    )]
                question = next(m["content"] for m in llm.calls[0][0] if m["role"] == "user")
                selected_area = json.loads(next(line for line in question.splitlines() if line.startswith('{"selected_area":')))["selected_area"]
                self.assertEqual(selected_area["spatial"], spatial)
                self.assertIn("不得仅查询观测站", question)
                self.assertEqual(len(llm.calls), 4)
                self.assertEqual(self.requests[0]["path"], "/api/spatial/v2/elements/list")
                self.assertEqual([r["payload"] for r in self.requests if r["method"] == "POST"], [
                    {**c["args"]["query"], "includeGeometry": True, "offset": 0, "limit": 20, "orderBy": "code"}
                    for c in query_calls
                ])
                finished = [event for event in events if event.type == "tool_finished"]
                self.assertEqual(len(finished), 5)
                self.assertTrue(all(event.success for event in finished))
                for event, item in zip(finished[-3:], self.results["list"]):
                    content = json.loads(event.content)
                    content.pop("map_result")
                    self.assertEqual(content, {
                        **self.element_results[item["dataKey"]], "operation": "query_spatial_element",
                    })
                actions = [event.data for event in events if event.type == "map_action"]
                self.assertEqual([action["action_type"] for action in actions], [
                    "CLEAR", "CLEAR", "MARKER_ADD", "CLEAR",
                ])
                self.assertEqual(actions[2]["payload"]["layer_id"], "spatial-element:test_point")
                self.assertEqual(actions[2]["payload"]["markers"][0]["coordinates"], [122.5, 31.3])
                self.assertTrue(all("clear_selection" not in action for action in actions))

    async def test_map_fetches_all_pages_preserving_multipart_geometry_and_scope(self):
        geometries = [
            {"type": "Point", "coordinates": [122, 30]},
            {"type": "LineString", "coordinates": [[122, 30], [123, 31]]},
            {"type": "MultiPolygon", "coordinates": [[
                [[122, 30], [123, 30], [123, 31], [122, 30]],
                [[122.5, 30.2], [122.8, 30.2], [122.8, 30.3], [122.5, 30.2]],
            ]]},
            None,
        ]
        features = [{"type": "Feature", "id": i, "geometry": geometry, "properties": {"name": str(i)}}
                    for i, geometry in enumerate(geometries)]
        self.element_results = {"test_point": {"type": "FeatureCollection", "features": features, "total": 4}}
        self.results["detail"]["maxQueryLimit"] = 2
        query = {
            "element": "test_point", "mode": "spatial", "limit": 20, "includeGeometry": False,
            "spatial": {"relation": "bbox", "bbox": {"west": 122, "south": 30, "east": 123, "north": 31}},
            "conditions": [{"field": "status", "operator": "=", "value": "运行"}],
        }
        original = deepcopy(query)
        result = await self.tool.execute("query_spatial_element", query=query)
        self.assertEqual(query, original)
        self.assertEqual([r["payload"] for r in self.requests if r["method"] == "POST"], [
            {**query, "includeGeometry": True, "limit": 2, "offset": offset, "orderBy": "code"}
            for offset in (0, 2)
        ])
        self.assertEqual(result.content["returned"], 2)
        self.assertEqual(result.content["map_result"], {
            "layer_id": "spatial-element:test_point",
            "geometry_layer_id": "spatial-element:test_point:shapes", "fetched": 4, "mapped": 3,
            "missing_geometry": 1, "complete": False, "status": "actions_sent",
        })
        shapes = [e for e in result.client_events if e["action_type"] == "GEOJSON_ADD"]
        self.assertEqual([e["payload"]["replace"] for e in shapes], [True, False])
        self.assertEqual([f for e in shapes for f in e["payload"]["geojson"]["features"]], features[1:3])
        self.assertEqual(result.client_events[1]["payload"]["markers"][0]["coordinates"], [122, 30])
        self.assertTrue(all(not e["payload"].get("fit_bounds", False) for e in result.client_events))

    async def test_map_rejects_incomplete_or_duplicate_pages_and_nonzero_offset(self):
        page = deepcopy(self.results["query"])
        page["total"] = 2
        failures = [
            SpatialServiceError("第二页请求失败"),
            {**page, "features": [], "returned": 0},
            {**page, "total": 3},
            {**page, "returned": 2},
            page,
        ]
        for second in failures:
            with self.subTest(second=second), patch.object(self.client, "query_spatial_element", AsyncMock(side_effect=[page, second])):
                with self.assertRaises((ValueError, SpatialServiceError)):
                    await self.tool.execute("query_spatial_element", query={"element": "test_point"}, render_map=True)
        with self.assertRaisesRegex(ValueError, "offset=0"):
            await self.tool.execute("query_spatial_element", query={"element": "test_point", "offset": 1}, render_map=True)

    async def test_empty_map_result_replaces_old_layer_without_clearing_selection(self):
        self.results["query"] = {"type": "FeatureCollection", "features": [], "total": 0, "returned": 0}
        result = await self.tool.execute("query_spatial_element", query={"element": "test_point"}, render_map=True)
        self.assertTrue(result.content["map_result"]["complete"])
        self.assertEqual(len(result.client_events), 1)
        self.assertEqual(result.client_events[0]["action_type"], "CLEAR")
        self.assertEqual(result.client_events[0]["payload"]["layer_ids"], [
            "spatial-element:test_point", "spatial-element:test_point:shapes",
        ])
        self.assertNotIn("clear_selection", result.client_events[0])

    async def test_station_points_use_existing_marker_layer_and_all_pages(self):
        features = [{
            "type": "Feature", "id": f"internal-{i}",
            "geometry": {"type": "Point", "coordinates": [122, 30]},
            "properties": {"tzno": str(i), "name": f"站点{i}", "station_type": "buoy"},
        } for i in range(41)]
        self.element_results = {"obs_station": {
            "type": "FeatureCollection", "features": features, "total": 41,
        }}
        result = await self.tool.execute("query_spatial_element", query={"element": "obs_station"}, render_map=True)
        self.assertEqual([e["action_type"] for e in result.client_events], ["CLEAR", "MARKER_ADD"])
        payload = result.client_events[1]["payload"]
        self.assertEqual(payload["layer_id"], "observation-stations")
        self.assertEqual(len(payload["markers"]), 41)
        self.assertEqual(payload["markers"][0]["id"], "0")
        self.assertEqual(payload["markers"][0]["type"], "buoy")
        self.assertEqual(result.content["returned"], 20)

    async def test_internal_id_column_is_not_injected_as_public_sort_field(self):
        self.results["detail"]["idColumn"] = "internal_id"
        self.results["query"]["total"] = 1
        await self.tool.execute("query_spatial_element", query={"element": "test_point"}, render_map=True)
        self.assertNotIn("orderBy", self.requests[-1]["payload"])

    async def test_transect_is_excluded_from_catalog_and_rejected_before_http(self):
        self.results["list"].append({"dataKey": "dm_duanmian", "dataName": "断面测点"})
        result = await self.tool.execute("list_spatial_elements")
        self.assertEqual([item["dataKey"] for item in result.content["elements"]], ["test_point"])
        self.requests.clear()
        with self.assertRaisesRegex(ValueError, "database_query"):
            await self.tool.execute("get_spatial_element_detail", dataKey="dm_duanmian")
        for render_map in (True, False):
            for mode in ("attribute", "spatial"):
                query = {"element": "dm_duanmian", "mode": mode}
                if mode == "spatial":
                    query["spatial"] = {"relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 1000}
                with self.assertRaisesRegex(ValueError, "database_query"):
                    await self.tool.execute("query_spatial_element", query=query, render_map=render_map)
        self.assertEqual(self.requests, [])
