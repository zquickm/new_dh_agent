import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.map_action import execute_map_action
from agents.tools.spatial_query_service import SpatialQueryServiceTool
from repositories.mysql_conversation_repository import MySQLConversationRepository
from schemas.chat import AgentRequest, FrontendContext
from services.chat_service import ChatService
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm


def call(name, **args):
    return ([], [{"id": "call-" + name, "name": name, "args": args}])


class SpatialContextFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = MemoryConversationRepository()
        self.conversation = await self.repo.create("空间连续问答", user_id="101")
        self.context = FrontendContext.model_validate({
            "drawn_geometry": {
                "type": "Circle", "center": [122.1, 30.2], "radius": 50000,
            },
        })
        for target, value in (
            ("services.chat_service.conversation_repository", self.repo),
            ("services.context_service.expand_query_synonyms", lambda value: value),
        ):
            patched = patch(target, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.tools = ToolExecutor()
        self.tools.register("map_action", "地图操作", execute_map_action)
        self.service = ChatService()
        self.addAsyncCleanup(self.service.close)

    def use_llm(self, responses):
        llm = FakeLlm(responses)
        self.service._agent = AgentLoop(llm, self.tools)
        return llm

    async def send(self, question, **extra):
        request = AgentRequest.model_validate({
            "question": question, "conversation_id": self.conversation.id, **extra,
        })
        events = [event async for event in self.service.stream_chat(request, user_id="101")]
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(all(event["event"] in {
            "context_usage",
            "conversation", "process", "retrieval", "map_action", "token", "done",
        } for event in events))
        return events

    async def test_explicit_clear_aliases_persist_without_restoring_history(self):
        llm = self.use_llm([(["回答"], [])] * 27)
        for alias in ("context", "map_context", "mapContext"):
            for value in (None, {}, {"drawn_geometry": None}):
                with self.subTest(alias=alias, value=value):
                    await self.send("框选区域有哪些观测站", **{alias: self.context.model_dump()})
                    await self.send("清空选区", **{alias: value})
                    await self.send("具体有哪些雷达站")
                    saved = await self.repo.get_map_context(self.conversation.id, user_id="101")
                    self.assertEqual(saved, FrontendContext())
                    for messages, _ in llm.calls[-2:]:
                        self.assertNotIn("selected_area", messages[-1]["content"])
                        self.assertIn("当前没有有效选区", messages[-1]["content"])
        detail = await self.repo.get(self.conversation.id, user_id="101")
        self.assertTrue(all("selected_area" not in item.content for item in detail.messages))
        self.assertTrue(all("当前空间范围" not in item.content for item in detail.messages))

    async def test_queries_and_explicit_map_actions_preserve_area(self):
        stations = [
            {"tzno": "R001", "name": "雷达站", "type": "radar", "lon": 122.1, "lat": 30.2},
            {"tzno": "B001", "name": "浮标站", "type": "buoy", "lon": 122.2, "lat": 30.3},
        ]
        def result(items):
            return {"type": "FeatureCollection", "total": len(items), "returned": len(items),
                    "features": [{"type": "Feature", "id": item["tzno"], "properties": item,
                                  "geometry": {"type": "Point", "coordinates": [item["lon"], item["lat"]]}}
                                 for item in items]}
        client = AsyncMock()
        client.query_spatial_element.side_effect = [result(stations), *[result(stations[:1])] * 3]
        self.tools.register("spatial_query_service", "通用空间查询服务", SpatialQueryServiceTool(client).execute)
        geometry = {"relation": "dwithin", "point": {"lon": 122.1, "lat": 30.2}, "distance": 50000}
        area_query = {"element": "obs_station", "mode": "spatial", "spatial": geometry, "includeGeometry": True}
        radar_filter = [{"field": "type", "operator": "=", "value": "radar"}]
        filtered_query = {**area_query, "conditions": radar_filter}
        detail_query = {**area_query, "conditions": [{"field": "tzno", "operator": "=", "value": "R001"}]}
        radar_query = call("spatial_query_service", operation="query_spatial_element", query=filtered_query)
        def draw(stations):
            return call("map_action", action_type="MARKER_ADD", payload={
                "layer_id": "observation-stations", "fit_bounds": False,
                "markers": [{"id": station["tzno"], "name": station["name"],
                             "coordinates": [station["lon"], station["lat"]]} for station in stations],
            })
        llm = self.use_llm([
            call("spatial_query_service", operation="query_spatial_element", query=area_query),
            draw(stations), (["区域内有雷达站和浮标站"], []),
            radar_query, draw(stations[:1]), (["有 R001 雷达站"], []),
            call("spatial_query_service", operation="query_spatial_element", query=detail_query),
            call("map_action", action_type="FLY_TO", payload={"center": [122.1, 30.2], "zoom": 12}),
            (["雷达站正常"], []),
            radar_query, draw(stations[:1]), (["雷达站位于原选区"], []),
        ])
        await self.send("有哪些观测站", map_context=self.context.model_dump())
        radar_events = await self.send("具体有哪些雷达站")
        station_events = await self.send("这个站点的详情")
        await self.send("其中雷达站的位置有哪些")
        for index in (0, 3, 9):
            question = llm.calls[index][0][-1]["content"]
            self.assertIn('"center":[122.1,30.2]', question)
            self.assertIn('"radius":50000.0', question)
        self.assertEqual([item.args[0] for item in client.query_spatial_element.call_args_list],
                         [area_query, filtered_query, detail_query, filtered_query])
        markers = next(event["data"]["payload"]["markers"] for event in radar_events
                       if event["event"] == "map_action" and event["data"]["action_type"] == "MARKER_ADD")
        self.assertEqual([marker["id"] for marker in markers], ["R001"])
        self.assertEqual([event["data"]["action_type"] for event in station_events
                          if event["event"] == "map_action"], ["CLEAR", "MARKER_ADD", "FLY_TO"])
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), self.context)

    async def test_visual_clear_and_flight_do_not_change_saved_selection(self):
        self.use_llm([
            (["区域内有观测站"], []),
            call("map_action", action_type="CLEAR", payload={"layer_ids": ["observation-stations"]}), (["标记已清除"], []),
            call("map_action", action_type="FLY_TO", payload={"center": [118.1, 24.5]}), (["定位指令已发送"], []),
        ])
        await self.send("框选区域有哪些站点", context=self.context.model_dump())
        await self.send("清除站点标记")
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), self.context)
        events = await self.send("改看厦门，飞到坐标 118.1,24.5")
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), self.context)
        flight = next(event["data"] for event in events
                      if event["event"] == "map_action" and event["data"]["action_type"] == "FLY_TO")
        self.assertEqual(flight["payload"]["center"], [118.1, 24.5])

    async def test_stale_run_cannot_clear_newer_selection(self):
        for run_id in ("old", "new"):
            await self.repo.begin_exchange(self.conversation.id, "框选", run_id=run_id, user_id="101")
        await self.repo.save_map_context(self.conversation.id, self.context, user_id="101", run_id="new")
        await self.repo.save_map_context(self.conversation.id, FrontendContext(), user_id="101", run_id="old")
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), self.context)
        self.assertIsNone(await self.repo.get_map_context(self.conversation.id, user_id="202"))

    async def test_place_switch_clears_scope_and_emits_existing_frontend_clear(self):
        llm = self.use_llm([
            (["原选区有观测站"], []),
            call("map_action", action_type="CLEAR", payload={}, clear_selection=True),
            (["厦门是福建省的城市"], []),
            (["请提供新范围"], []),
            (["新选区内有雷达站"], []),
        ])
        await self.send("有哪些观测站", context=self.context.model_dump())
        events = await self.send("介绍一下厦门")
        await self.send("这里有哪些雷达站")
        actions = [event["data"] for event in events if event["event"] == "map_action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action_type"], "CLEAR")
        self.assertEqual(actions[0]["payload"], {})
        self.assertLess(
            next(index for index, event in enumerate(events) if event["event"] == "map_action"),
            next(index for index, event in enumerate(events) if event["event"] == "token"),
        )
        self.assertNotIn("clear_selection", json.dumps(events, ensure_ascii=False))
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), FrontendContext())
        for messages, _ in llm.calls[2:]:
            current_question = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            self.assertNotIn("selected_area", current_question)
            self.assertNotIn("122.1", current_question)
            self.assertIn("当前没有有效选区", current_question)
        self.assertEqual(llm.calls[2][0][-1]["role"], "tool")
        self.assertEqual(llm.calls[2][0][-1]["name"], "map_action")
        follow_up = llm.calls[-1][0]
        self.assertIn("厦门是福建省的城市", [message["content"] for message in follow_up])
        new_context = {"drawn_geometry": {"type": "Circle", "center": [118.1, 24.5], "radius": 10000}}
        await self.send("改查厦门这个新框选区域的雷达站", map_context=new_context)
        self.assertIn('"center":[118.1,24.5]', llm.calls[-1][0][-1]["content"])
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"),
                         FrontendContext.model_validate(new_context))
        self.assertEqual(len(llm.calls), 5)

    async def test_unrelated_question_clears_selection_through_existing_tool(self):
        llm = self.use_llm([
            (["区域内有观测站"], []),
            call("map_action", action_type="CLEAR", payload={}, clear_selection=True),
            (["潮汐由引潮力产生"], []),
            (["请提供具体范围"], []),
        ])
        await self.send("有哪些观测站", context=self.context.model_dump())
        await self.send("解释潮汐原理")
        current_question = next(item["content"] for item in reversed(llm.calls[-1][0]) if item["role"] == "user")
        self.assertTrue(current_question.startswith("解释潮汐原理\n\n"))
        self.assertNotIn("selected_area", current_question)
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), FrontendContext())
        await self.send("具体有哪些雷达站")
        self.assertIn("当前没有有效选区", llm.calls[-1][0][-1]["content"])
        self.assertEqual(len(llm.calls), 4)

    async def test_selection_clear_with_layer_clear_preserves_sse_payload(self):
        await self.repo.save_map_context(self.conversation.id, self.context, user_id="101")
        payload = {"layer_ids": ["observation-stations"]}
        self.use_llm([
            call("map_action", action_type="CLEAR", payload=payload, clear_selection=True),
            (["已清除"], []),
        ])
        events = await self.send("清除选区和站点标记")
        actions = [event["data"] for event in events if event["event"] == "map_action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action_type"], "CLEAR")
        self.assertEqual(actions[0]["payload"], payload)
        self.assertEqual(set(actions[0]), {
            "version", "conversation_id", "run_id", "batch_id", "action_id", "seq", "action_type", "payload",
        })
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), FrontendContext())

    async def test_selection_remains_cleared_when_new_place_lookup_fails(self):
        async def lookup():
            raise RuntimeError("新地点查询失败")

        self.tools.register("lookup", "查询地点", lookup)
        await self.repo.save_map_context(self.conversation.id, self.context, user_id="101")
        llm = self.use_llm([
            call("map_action", action_type="CLEAR", payload={}, clear_selection=True),
            call("lookup"), (["未找到新地点数据"], []),
        ])
        await self.send("改查厦门")
        self.assertEqual(await self.repo.get_map_context(self.conversation.id, user_id="101"), FrontendContext())
        for messages, _ in llm.calls[1:]:
            current_question = next(item["content"] for item in reversed(messages) if item["role"] == "user")
            self.assertNotIn("selected_area", current_question)

    async def test_selection_persistence_failure_stops_before_model_continues(self):
        await self.repo.save_map_context(self.conversation.id, self.context, user_id="101")
        llm = self.use_llm([
            call("map_action", action_type="CLEAR", payload={}, clear_selection=True),
            (["不应继续回答"], []),
        ])
        with patch.object(self.repo, "save_map_context", side_effect=RuntimeError("保存失败")):
            events = [event async for event in self.service.stream_chat(
                AgentRequest(question="改查厦门", conversation_id=self.conversation.id), user_id="101",
            )]
        self.assertEqual(events[-1]["event"], "error")
        self.assertFalse(any(event["event"] in {"token", "done", "map_action"} for event in events))
        self.assertEqual(len(llm.calls), 1)

    async def test_mysql_map_context_write_checks_latest_run_under_conversation_lock(self):
        for run_id in ("old", "new"):
            with self.subTest(run_id=run_id):
                item = SimpleNamespace(map_context=self.context.model_dump(mode="json"), version=0)
                session = MagicMock()
                session.execute = AsyncMock(side_effect=[
                    SimpleNamespace(scalar_one_or_none=lambda: item),
                    SimpleNamespace(scalar_one_or_none=lambda: "new"),
                ])
                runtime = MagicMock()
                runtime.session_factory.return_value.__aenter__.return_value = session
                repo = MySQLConversationRepository(runtime)
                await repo.save_map_context(self.conversation.id, FrontendContext(), user_id="101", run_id=run_id)
                expected = self.context if run_id == "old" else FrontendContext()
                self.assertEqual(item.map_context, expected.model_dump(mode="json"))
                self.assertEqual(item.version, int(run_id == "new"))
                self.assertIn("FOR UPDATE", str(session.execute.call_args_list[0].args[0]))
