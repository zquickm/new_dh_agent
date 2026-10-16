import json
import asyncio
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from agents.agent_loop import AgentLoop
from agents.skills import SkillCatalog
from agents.tools.duanmian_element_compare import execute_duanmian_element_compare
from agents.tools.executor import ToolExecutor
from agents.tools.load_skill import load_skill
from agents.tools.map_action import execute_map_action, spatial_map_results
from agents.tools.spatial_query_service import (
    TOOL_DESCRIPTION, TOOL_NAME, TOOL_PARAMETERS, SpatialQueryServiceTool,
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
                "type": "FeatureCollection", "total": 1, "returned": 1,
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
        self.tool = SpatialQueryServiceTool(self.client)
        self.executor = ToolExecutor()
        self.executor.register(TOOL_NAME, TOOL_DESCRIPTION, self.tool.execute, TOOL_PARAMETERS)
        self.executor.register("map_action", "地图操作", execute_map_action)

    async def asyncTearDown(self):
        await self.tool.close()
        await self.runner.cleanup()

    async def test_operation_tool_names_use_canonical_calls_in_execution_audit_and_history(self):
        calls = [
            {"id": "list", "name": "list_spatial_elements", "args": {}},
            {"id": "detail", "name": "get_spatial_element_detail", "args": {"dataKey": "test_point"}},
            {"id": "query", "name": "query_spatial_element", "args": {"query": {"element": "test_point", "includeGeometry": True}}},
        ]
        original = deepcopy(calls)
        llm = FakeLlm([*[([], [call]) for call in calls], (["已取得目录及测试点。"], [])])
        agent = AgentLoop(llm, self.executor)
        recorder = AsyncMock()
        saved = []
        try:
            with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
                events = [event async for event in agent.run_stream(
                    "东海区立体观测体系概览", [], audit_recorder=recorder,
                    model_messages_callback=saved.append,
                )]
            finished = [event for event in events if event.type == "tool_finished"]
            self.assertEqual([event.success for event in finished], [True] * 3)
            self.assertTrue(all(event.tool_name == TOOL_NAME for event in finished))
            self.assertEqual([request["path"] for request in self.requests], [
                "/api/spatial/v2/elements/list", "/api/spatial/v2/elements/detail", "/api/spatial/v2/elements/query",
            ])
            self.assertEqual(self.requests[1]["params"], {"dataKey": "test_point"})
            self.assertEqual(self.requests[2]["payload"], calls[2]["args"]["query"])
            self.assertEqual(calls, original)
            for index, call in enumerate(calls):
                expected_args = {**call["args"], "operation": call["name"]}
                audit = recorder.started.await_args_list[index].kwargs
                self.assertEqual((audit["tool_name"], audit["arguments"]), (TOOL_NAME, expected_args))
                self.assertEqual(recorder.finished.await_args_list[index].kwargs["status"], "succeeded")
                assistant, result = saved[-1][index * 2:index * 2 + 2]
                self.assertEqual(assistant["tool_calls"][0]["name"], TOOL_NAME)
                self.assertEqual(assistant["tool_calls"][0]["args"], expected_args)
                self.assertEqual((result["name"], result["tool_call_id"]), (TOOL_NAME, call["id"]))
            self.assertNotIn("list_spatial_elements", {item["function"]["name"] for item in llm.calls[0][1]})
        finally:
            await agent.close()

    async def test_operation_name_correction_preserves_scope_and_argument_validation(self):
        cases = [
            ("查询目录", "list_spatial_elements", {"operation": "query_spatial_element"}, "TOOL_NOT_FOUND"),
            ("厦门查询目录", "list_spatial_elements", {}, "TOOL_NOT_FOUND"),
            ("查询目录", "invented_spatial_operation", {}, "TOOL_NOT_FOUND"),
            ("查询目录", "list_spatial_elements", {"unexpected": True}, "TOOL_ARGUMENT_INVALID"),
        ]
        for question, name, arguments, error_code in cases:
            with self.subTest(question=question, name=name, arguments=arguments):
                llm = FakeLlm([([], [{"id": "invalid", "name": name, "args": arguments}]), (["查询失败。"], [])])
                agent = AgentLoop(llm, self.executor)
                recorder = AsyncMock()
                try:
                    with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
                        events = [event async for event in agent.run_stream(question, [], audit_recorder=recorder)]
                    self.assertFalse(next(event for event in events if event.type == "tool_finished").success)
                    self.assertEqual(recorder.finished.await_args.kwargs["error_code"], error_code)
                    self.assertEqual(self.requests, [])
                finally:
                    await agent.close()

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
            "operation": "query_spatial_element", "query": query, "fetch_all": False,
        })
        result = json.loads(content)
        self.assertEqual(query, original)
        self.assertEqual(self.requests[-1]["payload"], original)
        self.assertEqual(self.requests[1]["params"], {"dataKey": "test_point"})
        self.assertEqual([r["method"] for r in self.requests], ["GET", "GET", "POST"])
        self.assertTrue(all(r["api_key"] == "test-key" for r in self.requests))
        self.assertEqual(result, {**self.results["query"], "operation": "query_spatial_element"})
        self.assertEqual(events, ())

    async def test_station_queries_use_catalog_fields_without_local_rewriting(self):
        self.results["list"] = [{"dataKey": "obs_station", "dataName": "观测站点"}]
        self.results["detail"] = {
            "dataKey": "obs_station", "idColumn": "tzno", "maxQueryLimit": 100,
            "fields": [
                {"name": "tzno", "type": "string", "operatorWhitelist": ["="]},
                {"name": "name", "type": "string", "operatorWhitelist": ["=", "LIKE"]},
                {"name": "station_kind", "type": "string", "enums": ["HF_RADAR"],
                 "enumLabels": ["雷达站"], "operatorWhitelist": ["="]},
            ],
        }
        # Preserve the service's identifiers, classifications and page totals,
        # including records without geometry and names with business suffixes.
        self.results["query"] = {
            "type": "FeatureCollection", "total": 31, "returned": 2,
            "features": [
                {"type": "Feature", "id": "R001", "geometry": None,
                 "properties": {"tzno": "R001", "name": "衢山平台", "station_kind": "HF_RADAR"}},
                {"type": "Feature", "id": "R001",
                 "geometry": {"type": "Point", "coordinates": [122.2, 30.3]},
                 "properties": {"tzno": "R001", "name": "衢山浮标", "station_kind": "HF_RADAR"}},
            ],
        }
        listing = await self.tool.execute("list_spatial_elements")
        element = listing.content["elements"][0]["dataKey"]
        detail = await self.tool.execute("get_spatial_element_detail", dataKey=element)
        kind = detail.content["fields"][2]
        queries = [
            {"element": element, "mode": "spatial", "includeGeometry": True,
             "spatial": {"relation": "dwithin", "point": {"lon": 122.2, "lat": 30.3}, "distance": 10000},
             "conditions": [{"field": kind["name"], "operator": "=", "value": kind["enums"][0]}],
             "limit": 2, "offset": 20},
            {"element": element, "mode": "attribute",
             "conditions": [{"field": "name", "operator": "=", "value": "衢山平台"}]},
            {"element": element, "mode": "attribute", "includeGeometry": True,
             "conditions": [{"field": detail.content["idColumn"], "operator": "=", "value": "R001"}]},
        ]
        for query in queries:
            with self.subTest(query=query):
                before = len(self.requests)
                result = await self.tool.execute("query_spatial_element", query=query, fetch_all=False)
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.requests[-1]["path"], "/api/spatial/v2/elements/query")
                self.assertEqual(self.requests[-1]["payload"], query)
                self.assertEqual(result.content, {"operation": "query_spatial_element", **self.results["query"]})
                self.assertEqual(result.client_events, ())

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
            for explicit_mode in (True, False):
                with self.subTest(relation=spatial["relation"], explicit_mode=explicit_mode):
                    query = {
                        "element": "test_point", "spatial": spatial,
                        "fields": ["code"], "includeGeometry": True,
                        "conditions": [{"field": "code", "operator": "=", "value": "E1001"}],
                        "orderBy": "code", "limit": 10, "offset": 20,
                    }
                    if explicit_mode:
                        query["mode"] = "spatial"
                    original = deepcopy(query)
                    before = len(self.requests)
                    result = await self.tool.execute("query_spatial_element", query=query, fetch_all=False)
                    self.assertEqual(len(self.requests), before + 1)
                    self.assertEqual(self.requests[-1]["payload"], {**original, "mode": "spatial"})
                    self.assertEqual(query, original)
                    self.assertEqual(result.client_events, ())

    async def test_islands_keep_generic_query_endpoint_scope_filters_and_map_link(self):
        self.results["list"] = [{"dataKey": "partner_island", "dataName": "海岛"}]
        listing = await self.tool.execute("list_spatial_elements")
        element = listing.content["elements"][0]["dataKey"]
        polygon = {"type": "Polygon", "coordinates": [[[122, 30], [123, 30], [122, 31], [122, 30]]]}
        self.results["query"] = {
            "type": "FeatureCollection", "total": 50, "returned": 1,
            "features": [{"type": "Feature", "id": "island-1", "geometry": polygon,
                          "properties": {"std_name": "测试海岛", "area_m2": 1234}}],
        }
        for spatial in (
            {"relation": "bbox", "bbox": {"west": 122, "south": 30, "east": 123, "north": 31}},
            {"relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 5000},
            {"relation": "intersects", "geometry": polygon},
        ):
            with self.subTest(relation=spatial["relation"]):
                query = {
                    "element": element, "mode": "spatial", "spatial": spatial,
                    "fields": ["std_name", "area_m2"],
                    "conditions": [{"field": "std_name", "operator": "LIKE", "value": "%测试%"}],
                    "orderBy": "area_m2", "orderDirection": "desc",
                    "includeGeometry": True, "limit": 10, "offset": 20,
                }
                original = deepcopy(query)
                before = len(self.requests)
                token = spatial_map_results.set({})
                try:
                    output = await self.tool.execute("query_spatial_element", query=query, fetch_all=False)
                    action = execute_map_action("GEOJSON_ADD", {"result_ref": output.content["result_ref"]})
                finally:
                    spatial_map_results.reset(token)
                self.assertEqual(self.requests[before:], [{
                    "method": "POST", "path": "/api/spatial/v2/elements/query", "params": {},
                    "payload": original, "api_key": "test-key",
                }])
                self.assertEqual(query, original)
                self.assertEqual(output.content["total"], 50)
                self.assertEqual(output.content["features"], self.results["query"]["features"])
                self.assertEqual(output.client_events, ())
                self.assertEqual(action.client_events[0]["payload"], {
                    "layer_id": f"spatial-element:{element}", "replace": False, "fit_bounds": False,
                    "geojson": {"type": "FeatureCollection", "features": self.results["query"]["features"]},
                })

    async def test_invalid_queries_fail_before_http_without_dropping_filters(self):
        invalid = [
            {}, {"element": " "}, {"element": "test_point", "mode": "spatial"},
            {"mode": "attribute", "spatial": {"relation": "bbox"}},
            *[{"mode": mode, "spatial": {
                "relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 5000,
            }} for mode in ("attribute", None, "invalid")],
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
                result = await self.tool.execute("query_spatial_element", query={"element": "test_point", "offset": 30}, fetch_all=False)
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
        catalog = SkillCatalog(Path(__file__).resolve().parents[1] / "skills")
        self.executor.register("load_skill", "加载能力", lambda name: "使用通用空间操作查询测试点位置。")
        calls = [
            ("load_skill", {"name": "空间联动"}),
            (TOOL_NAME, {"operation": "list_spatial_elements"}),
            (TOOL_NAME, {"operation": "get_spatial_element_detail", "dataKey": "test_point"}),
            (TOOL_NAME, {"operation": "query_spatial_element", "query": {"element": "test_point"}}),
        ]
        llm = FakeLlm([
            *[([], [{"id": f"call-{i}", "name": name, "args": args}]) for i, (name, args) in enumerate(calls)],
            (["已找到测试点 E1001。"], []),
        ])
        agent = AgentLoop(llm, self.executor, skills=catalog)
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
            {"dataKey": "dm_duanmian", "dataName": "断面测点"},
            {"dataKey": "test_area", "dataName": "测试面要素"},
        ]
        self.element_results = {
            "obs_station": {"type": "FeatureCollection", "total": 0, "returned": 0, "features": []},
            "dm_duanmian": {**self.results["query"], "total": 1},
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
                        "element": item["dataKey"], "mode": "spatial", "spatial": spatial, "includeGeometry": True,
                    }},
                } for item in self.results["list"]]
                llm = FakeLlm([
                    ([], [{"id": "skill", "name": "load_skill", "args": {"name": "空间联动"}}]),
                    ([], [{"id": "list", "name": TOOL_NAME, "args": {"operation": "list_spatial_elements"}}]),
                    ([], query_calls),
                    ([], [{"id": "map-points", "name": "map_action", "args": {
                        "action_type": "MARKER_ADD", "payload": {
                            "layer_id": "spatial-element:dm_duanmian", "fit_bounds": False,
                            "markers": [{"id": "E1001", "name": "E1001", "coordinates": [122.5, 31.3]}],
                        },
                    }}]),
                    (["断面测点1个；测试面要素1个缺少几何；观测站点0个。"], []),
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
                self.assertIn("区域总览应覆盖可查询的各类要素", question)
                self.assertNotIn("不纳入通用空间查询", question)
                self.assertEqual(len(llm.calls), 5)
                self.assertEqual(self.requests[0]["path"], "/api/spatial/v2/elements/list")
                self.assertEqual([r["payload"] for r in self.requests if r["method"] == "POST"], [
                    c["args"]["query"]
                    for c in query_calls
                ])
                finished = [event for event in events if event.type == "tool_finished"]
                self.assertEqual(len(finished), 6)
                self.assertTrue(all(event.success for event in finished))
                self.assertEqual(finished[-1].tool_name, "map_action")
                for event, item in zip(finished[2:5], self.results["list"]):
                    content = json.loads(event.content)
                    self.assertTrue(content.pop("result_ref").startswith("spatial-"))
                    self.assertTrue(content.pop("pagination")["complete"])
                    self.assertEqual(content, {
                        **self.element_results[item["dataKey"]], "operation": "query_spatial_element",
                    })
                actions = [event.data for event in events if event.type == "map_action"]
                self.assertEqual([action["action_type"] for action in actions], [
                    "MARKER_ADD",
                ])
                self.assertEqual(actions[0]["payload"]["layer_id"], "spatial-element:dm_duanmian")
                self.assertEqual(actions[0]["payload"]["markers"][0]["coordinates"], [122.5, 31.3])
                self.assertTrue(all("clear_selection" not in action for action in actions))
                self.assertTrue(all(event.tool_name == "map_action" for event in events if event.type == "map_action"))

    @patch("services.context_service.expand_query_synonyms", new=lambda text: text)
    async def test_transect_map_and_comparison_share_existing_action_flow(self):
        catalog = SkillCatalog(Path(__file__).resolve().parents[1] / "skills")
        self.executor.register("load_skill", "加载能力", lambda name: load_skill(catalog, name))
        self.executor.register("dm_duanmian_element_compare", "断面要素对比", execute_duanmian_element_compare)
        self.results["list"] = [{"dataKey": "test_transects", "dataName": "测试断面线"}]
        self.results["detail"] = {
            "dataKey": "test_transects", "maxQueryLimit": 100,
            "fields": [{"name": "section", "type": "string", "operatorWhitelist": ["="]}],
        }
        features = [{
            "type": "Feature", "id": section, "properties": {"section": section, "name": section + "断面"},
            "geometry": {"type": "LineString", "coordinates": [[122, 30 + index], [123, 30 + index]]},
        } for index, section in enumerate(("E1",))]
        self.element_results = {"test_transects": {"type": "FeatureCollection", "total": len(features), "features": features}}
        query = {
            "element": "test_transects", "includeGeometry": True, "limit": 1,
            "conditions": [{"field": "section", "operator": "=", "value": "E1"}],
        }
        months = ["2025-11", "2024-11"]

        class TransectLlm(FakeLlm):
            async def stream(self, messages, tools=None, temperature=0):
                # Consume the real query reference instead of copying geometry into the model call.
                result = next((json.loads(m["content"]) for m in messages
                               if m.get("tool_call_id") == "query-transects"), None)
                if result and not any(m.get("tool_call_id") == "map-transects" for m in messages):
                    self.responses = iter([
                        ([], [{"id": "map-transects", "name": "map_action", "args": {
                            "action_type": "GEOJSON_ADD", "payload": {"result_ref": result["result_ref"]},
                        }}]),
                        (["已提交 E1 断面地图及 E1007 测点对比指令。"], []),
                    ])
                async for item in super().stream(messages, tools=tools, temperature=temperature):
                    yield item

        llm = TransectLlm([
            ([], [{"id": "skill", "name": "load_skill", "args": {"name": "断面业务"}}]),
            ([], [{"id": "list", "name": TOOL_NAME, "args": {"operation": "list_spatial_elements"}}]),
            ([], [{"id": "detail", "name": TOOL_NAME, "args": {
                "operation": "get_spatial_element_detail", "dataKey": "test_transects",
            }}]),
            ([], [
                {"id": "query-transects", "name": TOOL_NAME, "args": {"operation": "query_spatial_element", "query": query}},
                {"id": "compare", "name": "dm_duanmian_element_compare", "args": {"months": months, "station": "E1007"}},
            ]),
        ])
        agent = AgentLoop(llm, self.executor, skills=catalog)
        events = [event async for event in agent.run_stream("2025年11月与2024年11月 E1 断面各类要素对比情况，浮层测点用 E1007", [])]
        self.assertTrue(all(event.success for event in events if event.type == "tool_finished"))
        actions = [event.data for event in events if event.type == "map_action"]
        self.assertEqual([action["action_type"] for action in actions], ["dm_duanmian_element_compare", "GEOJSON_ADD"])
        self.assertEqual(actions[0]["payload"], {"months": months, "station": "E1007"})
        self.assertEqual(actions[1]["payload"], {
            "layer_id": "spatial-element:test_transects", "replace": True, "fit_bounds": False,
            "geojson": {"type": "FeatureCollection", "features": features},
        })
        self.assertEqual([request["payload"] for request in self.requests if request["method"] == "POST"], [
            query,
        ])

    @patch("services.context_service.expand_query_synonyms", new=lambda text: text)
    async def test_result_references_render_original_pages_and_expire_after_run(self):
        features = [
            self.results["query"]["features"][0],
            {"type": "Feature", "id": "line", "properties": {"name": "航线", "value": 0},
             "geometry": {"type": "LineString", "coordinates": [[122, 30], [123, 31]]}},
            {"type": "Feature", "id": "missing", "properties": {"name": "无坐标"}, "geometry": None},
        ]
        self.element_results = {"test_point": {"type": "FeatureCollection", "total": 3, "features": features}}
        query = {"element": "test_point", "includeGeometry": True, "limit": 1}
        references = []

        class ReferenceLlm:
            model = "fake-model"

            async def stream(self, messages, tools=None, temperature=0):
                observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                if not observations:
                    yield "", [{"id": f"query-{offset}", "name": TOOL_NAME, "args": {
                        "operation": "query_spatial_element", "query": {**query, "offset": offset}, "fetch_all": False,
                    }} for offset in range(3)]
                elif len(observations) == 3:
                    references.extend(item["result_ref"] for item in observations)
                    payloads = [
                        # Model redundantly copies the first page's geometry.
                        {"result_ref": references[0], "geojson": observations[0]},
                        # Model copies the next page but omits its layer_id and reference.
                        {"geojson": observations[1]},
                        {"result_ref": references[2]},
                    ]
                    yield "", [
                        *[{"id": f"map-{i}", "name": "map_action", "args": {
                            "action_type": "GEOJSON_ADD", "payload": payload,
                        }} for i, payload in enumerate(payloads)],
                        {"id": "fly", "name": "map_action", "args": {
                            "action_type": "FLY_TO", "payload": {
                                "result_ref": references[0],
                                "geojson": {"type": "FeatureCollection", "features": observations[0]["features"]},
                            },
                        }},
                    ]
                else:
                    yield "已提交两条几何，一条缺少坐标。", []

        agent = AgentLoop(ReferenceLlm(), self.executor)
        events = []
        async for event in agent.run_stream("展示空间要素", []):
            self.assertIsNone(spatial_map_results.get())
            events.append(event)
        finished = [event for event in events if event.type == "tool_finished"]
        self.assertTrue(all(event.success for event in finished))
        self.assertEqual(len(set(references)), 3)
        actions = [event.data for event in events if event.type == "map_action"]
        self.assertEqual(len(actions), 6)
        for index, action in enumerate(actions[:3]):
            self.assertEqual(action["payload"], {
                "geojson": {"type": "FeatureCollection", "features": features[index:index + 1]},
                "layer_id": "spatial-element:test_point", "fit_bounds": False, "replace": index == 0,
            })
        self.assertEqual(actions[-2]["payload"]["geojson"]["features"], features[:1])
        self.assertEqual(actions[-1]["payload"]["center"], features[0]["geometry"]["coordinates"])
        self.assertEqual([r["payload"] for r in self.requests], [{**query, "offset": i} for i in range(3)])

        agent.llm = FakeLlm([
            ([], [{"id": "stale-map", "name": "map_action", "args": {
                "action_type": "GEOJSON_ADD", "payload": {"result_ref": references[0]},
            }}]),
            (["引用已过期，需要重新查询。"], []),
        ])
        events = [event async for event in agent.run_stream("继续展示", [])]
        self.assertFalse(any(event.type == "map_action" for event in events))
        failure = next(event for event in events if event.type == "tool_finished")
        self.assertFalse(failure.success)
        self.assertIn("已过期", failure.content)
        self.assertIsNone(spatial_map_results.get())

    @patch("services.context_service.expand_query_synonyms", new=lambda text: text)
    async def test_concurrent_runs_have_separate_reference_caches(self):
        caches = []
        both_started = asyncio.Event()

        async def query_and_check():
            cache = spatial_map_results.get()
            caches.append(cache)
            if len(caches) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=2)
            result = await self.tool.execute("query_spatial_element", query={
                "element": "obs_station", "includeGeometry": True,
            })
            self.assertEqual(list(cache), [result.content["result_ref"]])
            self.assertEqual(cache[result.content["result_ref"]]["layer_id"], "observation-stations")
            return result

        self.executor.register("query_and_check", "并发查询", query_and_check)
        llm = FakeLlm([
            ([], [{"id": "query", "name": "query_and_check", "args": {}}]),
            ([], [{"id": "query", "name": "query_and_check", "args": {}}]),
            (["完成"], []), (["完成"], []),
        ])
        agent = AgentLoop(llm, self.executor)

        async def run():
            return [event async for event in agent.run_stream("查询", [])]

        runs = await asyncio.gather(run(), run())
        self.assertIsNot(caches[0], caches[1])
        self.assertTrue(all(event.success for events in runs for event in events if event.type == "tool_finished"))
        self.assertIsNone(spatial_map_results.get())

    async def test_one_call_fetches_all_geometry_and_single_page_mode_still_works(self):
        features = [
            {"type": "Feature", "id": "line", "properties": {"name": "航线"},
             "geometry": {"type": "LineString", "coordinates": [[122, 30], [123, 31]]}},
            {"type": "Feature", "id": "area", "properties": {},
             "geometry": {"type": "Polygon", "coordinates": [[[122, 30], [123, 30], [123, 31], [122, 30]]]}},
            {"type": "Feature", "id": "missing", "properties": {}, "geometry": None},
        ]
        self.element_results = {"test_area": {"type": "FeatureCollection", "features": features, "total": 3}}
        query = {"element": "test_area", "mode": "spatial", "includeGeometry": True, "limit": 2,
                 "spatial": {"relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 1000}}
        original = deepcopy(query)
        token = spatial_map_results.set({})
        try:
            first = await self.tool.execute("query_spatial_element", query=query)
            action = execute_map_action("GEOJSON_ADD", {"result_ref": first.content["result_ref"]})
        finally:
            spatial_map_results.reset(token)
        self.assertEqual(query, original)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0]["payload"], query)
        self.assertEqual(self.requests[1]["payload"], {**query, "offset": 2})
        self.assertEqual(first.content["features"], features)
        self.assertEqual(first.content["returned"], 3)
        self.assertEqual(first.content["total"], 3)
        self.assertEqual(first.content["pagination"], {
            "complete": True, "pages": 2, "fetched": 3, "start_offset": 0, "next_offset": None,
        })
        self.assertNotIn("map_result", first.content)
        self.assertEqual(first.client_events, ())
        self.assertEqual(action.client_events[0]["payload"], {
            "layer_id": "spatial-element:test_area", "replace": True, "fit_bounds": False,
            "geojson": {"type": "FeatureCollection", "features": features},
        })
        second = await self.tool.execute("query_spatial_element", query={**query, "offset": 2}, fetch_all=False)
        self.assertEqual(second.content["features"], features[2:])
        self.assertNotIn("pagination", second.content)
        self.assertEqual(second.client_events, ())
        self.assertEqual(self.requests[-1]["payload"], {**query, "offset": 2})
        _, actions = await self.executor.execute_with_events("map_action", {
            "action_type": "GEOJSON_ADD", "payload": {
                "layer_id": "test_area", "fit_bounds": False,
                "geojson": {"type": "FeatureCollection", "features": first.content["features"]},
            },
        })
        self.assertEqual(actions[0]["action_type"], "GEOJSON_ADD")
        self.assertEqual(actions[0]["payload"]["geojson"]["features"], features)

    async def test_default_call_collects_more_than_twenty_records_and_preserves_all_filters(self):
        features = [{
            "type": "Feature", "id": index, "geometry": {"type": "Point", "coordinates": [122 + index / 1000, 30]},
            "properties": {"std_name": f"测试海岛{index}", "area": index},
        } for index in range(65)]
        self.element_results = {"partner_island": {"type": "FeatureCollection", "features": features, "total": 65}}
        query = {
            "element": "partner_island", "mode": "spatial", "includeGeometry": True,
            "spatial": {"relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 5000},
            "fields": ["std_name", "area"], "conditions": [{"field": "area", "operator": ">=", "value": 0}],
            "orderBy": "area", "orderDirection": "asc",
        }
        original = deepcopy(query)

        class AllPagesLlm:
            model = "fake-model"

            async def stream(self, messages, tools=None, temperature=0):
                observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                if not observations:
                    yield "", [{"id": "query-all", "name": TOOL_NAME, "args": {
                        "operation": "query_spatial_element", "query": query,
                    }}]
                elif len(observations) == 1:
                    yield "", [{"id": "map-all", "name": "map_action", "args": {
                        "action_type": "GEOJSON_ADD", "payload": {"result_ref": observations[0]["result_ref"]},
                    }}]
                else:
                    yield "已取得全部65条海岛数据并提交地图展示。", []

        with patch("services.context_service.expand_query_synonyms", side_effect=lambda text: text):
            events = [event async for event in AgentLoop(AllPagesLlm(), self.executor).run_stream("查询选区内全部海岛", [])]
        finished = [event for event in events if event.type == "tool_finished"]
        self.assertEqual([event.tool_name for event in finished], [TOOL_NAME, "map_action"])
        self.assertTrue(all(event.success for event in finished))
        output = json.loads(finished[0].content)
        self.assertEqual(output["features"], features)
        self.assertEqual(output["returned"], 65)
        self.assertTrue(output["pagination"]["complete"])
        self.assertEqual(output["pagination"]["pages"], 4)
        actions = [event.data for event in events if event.type == "map_action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["payload"]["geojson"]["features"], features)
        self.assertTrue(actions[0]["payload"]["replace"])
        self.assertEqual(actions[0]["payload"]["layer_id"], "spatial-element:partner_island")
        self.assertEqual(query, original)
        self.assertEqual([r["payload"] for r in self.requests], [
            query, *[{**query, "offset": offset} for offset in (20, 40, 60)],
        ])

    async def test_transect_catalog_detail_and_queries_reach_spatial_service(self):
        self.results["list"].append({"dataKey": "dm_duanmian", "dataName": "断面测点"})
        result = await self.tool.execute("list_spatial_elements")
        self.assertEqual(result.content["elements"], self.results["list"])
        self.results["detail"]["dataKey"] = "dm_duanmian"
        detail = await self.tool.execute("get_spatial_element_detail", dataKey="dm_duanmian")
        self.assertEqual(detail.content["dataKey"], "dm_duanmian")
        self.assertEqual(self.requests[-1]["params"], {"dataKey": "dm_duanmian"})
        self.results["query"]["total"] = 1
        for mode in ("attribute", "spatial"):
            with self.subTest(mode=mode):
                query = {"element": "dm_duanmian", "mode": mode}
                if mode == "spatial":
                    query["spatial"] = {"relation": "dwithin", "point": {"lon": 122, "lat": 30}, "distance": 1000}
                result = await self.tool.execute("query_spatial_element", query=query)
                self.assertEqual(result.content["features"], self.results["query"]["features"])
                self.assertEqual(self.requests[-1]["payload"], query)
                self.assertEqual(result.client_events, ())
