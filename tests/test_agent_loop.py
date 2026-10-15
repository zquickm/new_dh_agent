import asyncio
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from copy import deepcopy

from services.synonym_service import build_synonym_matcher
from agents.agent_loop import AgentLoop
from agents.skills import SkillCatalog
from services.context_service import SYSTEM_PROMPT
from agents.tools.executor import ToolExecutor, ToolOutput
from agents.tools.registry import build_tool_executor
from schemas.chat import ChatMessage


class FakeLlm:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.temperatures = []

    async def stream(self, messages, tools=None, temperature=0):
        self.temperatures.append(temperature)
        self.calls.append((deepcopy(messages), tools))
        content_parts, tool_calls = next(self.responses)
        for content in content_parts:
            yield content, []
        if tool_calls:
            yield "", tool_calls


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_round_budget_allows_nineteen_tool_rounds_then_forces_answer(self):
        calls = []

        async def echo(value):
            calls.append(value)
            return value

        llm = FakeLlm([
            ([], [{"id": f"call-{i}", "name": "Echo", "args": {"value": str(i)}}])
            for i in range(19)
        ] + [(["完成"], [])])
        tools = ToolExecutor()
        tools.register("Echo", "回显", echo)
        agent = AgentLoop(llm, tools)
        self.addAsyncCleanup(agent.close)
        self.assertEqual(agent.max_rounds, 20)
        events = [event async for event in agent.run_stream("执行多次查询", [])]
        self.assertEqual(calls, [str(i) for i in range(19)])
        self.assertEqual(len(llm.calls), 20)
        self.assertIsNotNone(llm.calls[18][1])
        self.assertIsNone(llm.calls[19][1])
        self.assertEqual("".join(event.content for event in events if event.type == "answer_delta"), "完成")

    async def test_rag_evidence_is_local_to_user_turn_and_history_keeps_status(self):
        evidence = "仅本次检索可见的证据正文"

        async def rag_search(query):
            return {"chunks": [{"text": evidence, "source": "资料.pdf"}]}

        async def query(question):
            return {"success": True, "rows": [{"value": 1}]}

        llm = FakeLlm([
            ([], [{"id": "rag", "name": "rag_search", "args": {"query": "资料"}}]),
            ([], [{"id": "db", "name": "database_query", "args": {"question": "数据"}}]),
            (["报告结论<<SRC_1>>"], []),
            (["需要重新检索。"], []),
        ])
        tools = ToolExecutor()
        tools.register("rag_search", "检索", rag_search)
        tools.register("database_query", "查询", query)
        agent = AgentLoop(llm, tools)
        saved = []
        try:
            events = [event async for event in agent.run_stream(
                "查询资料", [], model_messages_callback=lambda batch: saved.append(batch),
            )]
            for messages, _ in llm.calls[1:3]:
                self.assertNotIn(evidence, messages[0]["content"])
                result = next(item for item in messages if item.get("tool_call_id") == "rag")
                self.assertEqual(json.loads(result["content"])["chunks"][0]["text"], evidence)
                self.assertEqual(json.loads(result["content"])["chunks"][0]["source_key"], "<<SRC_1>>")
            self.assertEqual("".join(event.content for event in events if event.type == "answer_delta"),
                             "报告结论[[cite:1]]")
            history = [ChatMessage(role="user", content="查询资料")]
            history.extend(ChatMessage.model_validate(item) for item in saved[-1])
            self.assertNotIn(evidence, json.dumps(saved, ensure_ascii=False))
            _ = [event async for event in agent.run_stream("继续提问", history)]
            next_messages = llm.calls[-1][0]
            result = next(item for item in next_messages if item.get("tool_call_id") == "rag")
            self.assertEqual(json.loads(result["content"]), {"success": True})
            self.assertNotIn(evidence, json.dumps(next_messages, ensure_ascii=False))
            self.assertNotIn("<<SRC_1>>", json.dumps(next_messages, ensure_ascii=False))
        finally:
            await agent.close()

    async def test_spatial_and_station_failures_identify_operation_without_map_events(self):
        from tool_backends.spatial_backend import SpatialServiceError

        cases = [
            (operation, SpatialServiceError("空间服务返回 HTTP 400 (POST /api/spatial/v2/elements/query)：字段不支持"), "通用空间查询服务调用失败")
            for operation in ("list_spatial_elements", "get_spatial_element_detail", "query_spatial_element")
        ] + [
            ("station_status", SpatialServiceError("空间服务返回 HTTP 400"), "通用空间查询服务调用失败"),
            ("query_spatial_element", ValueError("空间服务分页计数无效"), "通用空间查询服务参数或返回结果无效"),
            ("observation_summary", ValueError("tzno 不能为空"), "通用空间查询服务参数或返回结果无效"),
        ]
        for operation, error, prefix in cases:
            with self.subTest(operation=operation, error=type(error).__name__):
                async def fail(**arguments):
                    raise error

                tools = ToolExecutor()
                tools.register("spatial_query_service", "查询", fail)
                llm = FakeLlm([
                    ([], [{"id": "failed-query", "name": "spatial_query_service", "args": {"operation": operation}}]),
                    (["查询未成功"], []),
                ])
                agent = AgentLoop(llm, tools)
                try:
                    events = [event async for event in agent.run_stream("查询", [])]
                    finished = next(event for event in events if event.type == "tool_finished")
                    self.assertFalse(finished.success)
                    self.assertTrue(finished.content.startswith(prefix))
                    self.assertIn(f"operation={operation}", finished.content)
                    self.assertIn(str(error), finished.content)
                    self.assertFalse(any(event.type == "map_action" for event in events))
                    self.assertTrue(any(
                        message.get("role") == "tool" and message["content"] == finished.content
                        for message in llm.calls[1][0]
                    ))
                finally:
                    await agent.close()

    async def test_all_skill_switch_combinations_preserve_tools(self):
        names = ("空间联动", "断面业务")
        with tempfile.TemporaryDirectory() as directory:
            for name in names:
                path = Path(directory) / name
                path.mkdir()
                (path / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: {name}独立摘要\n---\n{name}完整操作说明",
                    encoding="utf-8",
                )
            catalog = SkillCatalog(Path(directory))
            for enabled in (frozenset(), frozenset(names[:1]), frozenset(names[1:]), frozenset(names)):
                with self.subTest(enabled=enabled):
                    tools = build_tool_executor(catalog)
                    tools.register("database_query", "查询", lambda: "数据库仍可用")
                    definitions = tools.definitions()
                    calls = [
                        {"id": str(index), "name": "load_skill", "args": {"name": name}}
                        for index, name in enumerate(names)
                    ]
                    calls.append({"id": "db", "name": "database_query", "args": {}})
                    llm = FakeLlm([([], calls), (["完成"], [])])
                    agent = AgentLoop(llm, tools, catalog)
                    try:
                        events = [event async for event in agent.run_stream(
                            "测试能力开关", [], enabled_skill_names=enabled,
                        )]
                        prompt = llm.calls[0][0][0]["content"]
                        for name in names:
                            self.assertEqual(f"{name}独立摘要" in prompt, name in enabled)
                            self.assertEqual(
                                any(f"{name}完整操作说明" in message["content"]
                                    for message in llm.calls[1][0]),
                                name in enabled,
                            )
                        self.assertEqual(llm.calls[0][1], [
                            item for item in definitions
                            if item["function"]["name"] not in {
                                "analyze_suspicion", "query_suspicion_results", "summarize_suspicion_results",
                            }
                        ])
                        self.assertEqual(tools.definitions(), definitions)
                        finished = [event for event in events if event.type == "tool_finished"]
                        self.assertEqual([event.success for event in finished],
                                         [name in enabled for name in names] + [True])
                    finally:
                        await agent.close()

    async def test_interrupted_tool_batch_does_not_persist_orphan_calls(self):
        saved = []
        tools = ToolExecutor()
        tools.register("load_skill", "加载", lambda: "完整 Skill 说明")

        async def interrupted():
            raise asyncio.CancelledError()

        tools.register("interrupted", "中断", interrupted)
        llm = FakeLlm([
            ([], [{"id": "complete", "name": "load_skill", "args": {}}]),
            ([], [
                {"id": "partial", "name": "load_skill", "args": {}},
                {"id": "cancelled", "name": "interrupted", "args": {}},
            ]),
        ])
        agent = AgentLoop(llm, tools)
        try:
            with self.assertRaises(asyncio.CancelledError):
                _ = [event async for event in agent.run_stream(
                    "问题", [], model_messages_callback=saved.append,
                )]
            self.assertEqual(len(saved), 1)
            self.assertEqual([message["role"] for message in saved[0]], ["assistant", "tool"])
            self.assertEqual(saved[0][0]["tool_calls"][0]["id"], "complete")
            self.assertEqual(saved[0][1]["tool_call_id"], "complete")
        finally:
            await agent.close()

    def setUp(self):
        synonym_patch = patch(
            "services.synonym_service._active_synonym_matcher",
            build_synonym_matcher([
                {"standardTerm": "溶解氧", "terms": [{"term": "DO"}]},
            ]),
        )
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)

    def test_system_prompt_delegates_business_routing_to_skills(self):
        self.assertIn("当前任务相关的 Skill", SYSTEM_PROMPT)
        self.assertNotIn("必须优先调用 spatial_query_service", SYSTEM_PROMPT)
        self.assertNotIn("也不得改用 database_query", SYSTEM_PROMPT)

    async def test_station_query_keeps_generic_database_tool_available(self):
        llm = FakeLlm([(["已完成"], [])])
        agent = AgentLoop(llm, build_tool_executor())

        _ = [event async for event in agent.run_stream("小洋山站水温多少", [])]

        definitions = llm.calls[0][1]
        tool_names = {item["function"]["name"] for item in definitions}
        self.assertIn("spatial_query_service", tool_names)
        self.assertIn("database_query", tool_names)
        await agent.close()

    async def test_historical_transect_query_keeps_database_tool(self):
        llm = FakeLlm([(["已完成"], [])])
        agent = AgentLoop(llm, build_tool_executor())

        _ = [
            event
            async for event in agent.run_stream("查询 E2 断面调查水温数据", [])
        ]

        definitions = llm.calls[0][1]
        tool_names = {item["function"]["name"] for item in definitions}
        self.assertIn("database_query", tool_names)
        await agent.close()

    async def test_answers_directly_without_route_call(self):
        llm = FakeLlm([(["直接", "回答"], [])])
        tools = build_tool_executor()
        agent = AgentLoop(llm, tools)

        events = [
            event
            async for event in agent.run_stream(
                "解释赤潮",
                [ChatMessage(role="user", content="此前问题")],
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            ["answer_delta", "answer_delta"],
        )
        self.assertEqual("".join(event.content for event in events), "直接回答")
        messages, definitions = llm.calls[0]
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "user"],
        )
        self.assertEqual(messages[-1]["content"], "解释赤潮")
        self.assertTrue(all(item["type"] == "function" for item in definitions))
        self.assertEqual(len(llm.calls), 1)
        self.assertNotIn("READY_TO_ANSWER", messages[0]["content"])
        await agent.close()

    async def test_uses_managed_system_prompt_for_the_whole_run(self):
        llm = FakeLlm([(["完成"], [])])
        agent = AgentLoop(llm, build_tool_executor())

        _ = [
            event
            async for event in agent.run_stream(
                "测试在线提示词", [], system_prompt="当前发布版提示词"
            )
        ]

        self.assertTrue(
            llm.calls[0][0][0]["content"].startswith("当前发布版提示词\n\n")
        )
        self.assertIn("当前可用能力", llm.calls[0][0][0]["content"])
        await agent.close()

    async def test_native_tool_call_is_executed_before_answer(self):
        tool_call = {
            "id": "call-time",
            "name": "CurrentServerTime",
            "args": {},
            "type": "tool_call",
        }
        llm = FakeLlm(
            [
                ([], [tool_call]),
                (["当前服务器时间已获取"], []),
            ]
        )
        agent = AgentLoop(llm, build_tool_executor())

        events = [event async for event in agent.run_stream("现在几点了", [])]

        self.assertEqual(
            [event.type for event in events],
            ["tool_started", "tool_finished", "answer_delta"],
        )
        self.assertEqual(events[0].tool_name, "CurrentServerTime")
        self.assertEqual(events[0].tool_call_id, "call-time")
        self.assertEqual(events[1].tool_call_id, "call-time")
        self.assertTrue(events[1].success)
        self.assertIn('"datetime"', events[1].content)
        second_messages = llm.calls[1][0]
        self.assertEqual(second_messages[-2]["role"], "assistant")
        self.assertEqual(second_messages[-2]["tool_calls"], [tool_call])
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertEqual(second_messages[-1]["tool_call_id"], "call-time")
        self.assertIn('"datetime"', second_messages[-1]["content"])
        await agent.close()

    async def test_streams_and_keeps_text_from_every_round(self):
        tool_call = {
            "id": "call-time",
            "name": "CurrentServerTime",
            "args": {},
            "type": "tool_call",
        }
        llm = FakeLlm(
            [
                (["正在查询"], [tool_call]),
                (["查询完成"], []),
            ]
        )
        agent = AgentLoop(llm, build_tool_executor())

        events = [event async for event in agent.run_stream("现在几点了", [])]

        self.assertEqual(
            "".join(event.content for event in events if event.type == "answer_delta"),
            "正在查询\n\n查询完成",
        )
        self.assertEqual(
            [event.type for event in events],
            [
                "answer_delta",
                "tool_started",
                "tool_finished",
                "answer_delta",
                "answer_delta",
            ],
        )
        await agent.close()

    async def test_can_call_multiple_tools_across_rounds(self):
        responses = [
            (
                [],
                [
                    {
                        "id": "call-rag",
                        "name": "rag_search",
                        "args": {"query": "E2断面相关报告", "top_k": 5},
                        "type": "tool_call",
                    }
                ],
            ),
            (
                [],
                [
                    {
                        "id": "call-database",
                        "name": "database_query",
                        "args": {
                            "question": "查询E2断面盐度平均值",
                            "limit": 100,
                        },
                        "type": "tool_call",
                    }
                ],
            ),
            (["综合回答<<SRC_", "1>>"], []),
        ]

        async def rag_search(query, top_k=5):
            return {
                "query": query,
                "top_k": top_k,
                "chunks": [
                    {
                        "text": "报告依据",
                        "source": "E2报告.pdf",
                        "score": 0.91,
                        "document_id": "doc-1",
                        "chunk_id": "chunk-1",
                        "knowledge_id": "kb-1",
                        "title": "E2断面",
                        "url": None,
                        "page_index": 2,
                        "line": None,
                        "page": 3,
                    }
                ],
            }

        async def database_query(question, limit=100):
            return {"question": question, "limit": limit, "rows": [{"avg": 30.1}]}

        llm = FakeLlm(responses)
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        tools.register("database_query", "查询实际数据", database_query)
        agent = AgentLoop(llm, tools)

        events = [
            event
            async for event in agent.run_stream(
                "结合E2断面报告和实际盐度平均值进行分析",
                [],
            )
        ]

        self.assertEqual(
            [event.type for event in events],
            [
                "tool_started",
                "retrieval",
                "tool_finished",
                "tool_started",
                "tool_finished",
                "answer_delta",
                "answer_delta",
            ],
        )
        self.assertEqual(
            [events[2].tool_name, events[4].tool_name],
            ["rag_search", "database_query"],
        )
        self.assertEqual(events[1].data["tool_call_id"], "call-rag")
        self.assertEqual(events[1].data["result_count"], 1)
        self.assertEqual(events[1].data["chunks"][0]["preview"], "报告依据")
        self.assertEqual(events[1].data["chunks"][0]["page_index"], 2)
        self.assertIsNone(events[1].sources[0]["citation_id"])
        self.assertEqual(
            "".join(event.content for event in events if event.type == "answer_delta"),
            "综合回答[[cite:1]]",
        )
        final_messages = llm.calls[-1][0]
        rag_tool_message = next(
            item for item in final_messages
            if item.get("tool_call_id") == "call-rag"
        )
        self.assertIn("报告依据", rag_tool_message["content"])
        self.assertNotIn("报告依据", final_messages[0]["content"])
        self.assertEqual(json.loads(rag_tool_message["content"])["chunks"][0]["source_key"], "<<SRC_1>>")
        self.assertEqual(
            sum("报告依据" in str(item.get("content") or "") for item in final_messages),
            1,
        )
        self.assertIn('"avg": 30.1', final_messages[-1]["content"])

    async def test_explicit_map_actions_are_not_suppressed_after_other_events(self):
        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-observation",
                            "name": "spatial_query_service",
                            "args": {},
                            "type": "tool_call",
                        }
                    ],
                ),
                (
                    ["接下来定位地图"],
                    [
                        {
                            "id": "call-map",
                            "name": "map_action",
                            "args": {},
                            "type": "tool_call",
                        }
                    ],
                ),
                (["查询和定位完成"], []),
            ]
        )

        async def spatial_query_service():
            return ToolOutput(
                content={"total": 1},
                client_events=(
                    {"action_type": "CLEAR", "payload": {}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": []}},
                    {
                        "action_type": "FLY_TO",
                        "payload": {"center": [121.4737, 31.2304], "zoom": 12},
                    },
                ),
            )

        async def map_action():
            return ToolOutput(
                content={"success": True},
                client_events=(
                    {"action_type": "CLEAR", "payload": {}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": []}},
                    {
                        "action_type": "FLY_TO",
                        "payload": {"center": [121.4737, 31.2304], "zoom": 12},
                    },
                ),
            )

        tools = ToolExecutor()
        tools.register("spatial_query_service", "通用空间查询服务", spatial_query_service)
        tools.register("map_action", "地图操作", map_action)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("查询并定位", [])]

        self.assertEqual(
            [event.type for event in events],
            [
                "tool_started",
                "map_action",
                "map_action",
                "map_action",
                "tool_finished",
                "answer_delta",
                "tool_started",
                "map_action",
                "map_action",
                "map_action",
                "tool_finished",
                "answer_delta",
                "answer_delta",
            ],
        )
        self.assertEqual(
            [
                event.data["action_type"]
                for event in events
                if event.type == "map_action"
            ],
            ["CLEAR", "MARKER_ADD", "FLY_TO"] * 2,
        )
        self.assertEqual(
            "".join(
                event.content for event in events if event.type == "answer_delta"
            ),
            "接下来定位地图\n\n查询和定位完成",
        )

    async def test_keeps_standalone_fly_to_map_events(self):
        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-map",
                            "name": "map_action",
                            "args": {},
                            "type": "tool_call",
                        }
                    ],
                ),
                (["定位完成"], []),
            ]
        )

        async def map_action():
            return ToolOutput(
                content={"success": True},
                client_events=(
                    {"action_type": "CLEAR", "payload": {}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": []}},
                    {
                        "action_type": "FLY_TO",
                        "payload": {"center": [121.4737, 31.2304], "zoom": 12},
                    },
                ),
            )

        tools = ToolExecutor()
        tools.register("map_action", "地图操作", map_action)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("定位上海", [])]

        self.assertEqual(
            [
                event.data["action_type"]
                for event in events
                if event.type == "map_action"
            ],
            ["CLEAR", "MARKER_ADD", "FLY_TO"],
        )

    async def test_query_then_explicit_flight_emits_map_actions(self):
        llm = FakeLlm(
            [
                (
                    [],
                    [{
                        "id": "call-area",
                        "name": "spatial_query_service",
                        "args": {},
                        "type": "tool_call",
                    }],
                ),
                (
                    [],
                    [{
                        "id": "call-map",
                        "name": "map_action",
                        "args": {},
                        "type": "tool_call",
                    }],
                ),
                (["定位完成"], []),
            ]
        )

        async def spatial_query_service():
            return ToolOutput(content={"total": 2})

        async def map_action():
            return ToolOutput(
                content={"success": True},
                client_events=(
                    {"action_type": "CLEAR", "payload": {}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": [1]}},
                    {
                        "action_type": "FLY_TO",
                        "payload": {"center": [121.5, 31.2], "zoom": 12},
                    },
                ),
            )

        tools = ToolExecutor()
        tools.register("spatial_query_service", "通用空间查询服务", spatial_query_service)
        tools.register("map_action", "地图操作", map_action)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("查询后定位", [])]

        self.assertEqual(
            [
                event.data["action_type"]
                for event in events
                if event.type == "map_action"
            ],
            ["CLEAR", "MARKER_ADD", "FLY_TO"],
        )

    async def test_keeps_later_visualization_tool_events(self):
        llm = FakeLlm(
            [
                (
                    [],
                    [{
                        "id": "call-area",
                        "name": "render_result",
                        "args": {"operation": "area"},
                        "type": "tool_call",
                    }],
                ),
                (
                    [],
                    [{
                        "id": "call-station",
                        "name": "render_result",
                        "args": {"operation": "station"},
                        "type": "tool_call",
                    }],
                ),
                (["单站查询完成"], []),
            ]
        )

        async def render_result(operation):
            if operation == "area":
                client_events = (
                    {"action_type": "CLEAR", "payload": {}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": [1, 2]}},
                )
            else:
                client_events = (
                    {"action_type": "CLEAR", "payload": {}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": [1]}},
                    {
                        "action_type": "FLY_TO",
                        "payload": {"center": [121.4737, 31.2304], "zoom": 12},
                    },
                )
            return ToolOutput(
                content={"operation": operation},
                client_events=client_events,
            )

        tools = ToolExecutor()
        tools.register("render_result", "测试展示工具", render_result)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("先查区域再查单站", [])]

        self.assertEqual(
            [
                event.data["action_type"]
                for event in events
                if event.type == "map_action"
            ],
            ["CLEAR", "MARKER_ADD", "CLEAR", "MARKER_ADD", "FLY_TO"],
        )

    async def test_final_answer_streams_before_the_model_finishes(self):
        class DelayedLlm:
            model = "fake-model"

            def __init__(self):
                self.call_count = 0
                self.release = asyncio.Event()

            async def stream(self, messages, tools=None, temperature=0):
                del messages, temperature
                self.call_count += 1
                if self.call_count == 1:
                    yield "", [
                        {
                            "id": "call-echo",
                            "name": "Echo",
                            "args": {"value": "完成"},
                            "type": "tool_call",
                        }
                    ]
                    return
                yield "第一段", []
                await self.release.wait()
                yield "第二段", []

        llm = DelayedLlm()
        tools = ToolExecutor()
        tools.register("Echo", "回显", lambda value: value)
        agent = AgentLoop(llm, tools)
        stream = agent.run_stream("测试流式输出", [])

        self.assertEqual((await stream.__anext__()).type, "tool_started")
        self.assertEqual((await stream.__anext__()).type, "tool_finished")
        first_answer = await asyncio.wait_for(stream.__anext__(), timeout=1)
        self.assertFalse(llm.release.is_set())

        llm.release.set()
        remaining = [event async for event in stream]
        self.assertEqual(first_answer.type, "answer_delta")
        self.assertEqual(first_answer.content, "第一段")
        self.assertEqual([event.content for event in remaining], ["第二段"])

    async def test_final_answer_streams_citation_before_the_model_finishes(self):
        class DelayedCitationLlm:
            model = "fake-model"

            def __init__(self):
                self.call_count = 0
                self.calls = []
                self.release = asyncio.Event()

            async def stream(self, messages, tools=None, temperature=0):
                del temperature
                self.call_count += 1
                self.calls.append((deepcopy(messages), tools))
                if self.call_count == 1:
                    yield "", [
                        {
                            "id": "call-rag",
                            "name": "rag_search",
                            "args": {"query": "测试引用"},
                            "type": "tool_call",
                        }
                    ]
                    return
                yield "第一段<<SRC_", []
                await self.release.wait()
                yield "1>>第二段", []

        async def rag_search(query):
            return {
                "query": query,
                "chunks": [
                    {
                        "text": "引用依据",
                        "source": "资料.pdf",
                        "score": 0.9,
                        "page_index": 0,
                    }
                ],
            }

        llm = DelayedCitationLlm()
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        agent = AgentLoop(llm, tools)
        stream = agent.run_stream("测试流式引用", [])

        self.assertEqual((await stream.__anext__()).type, "tool_started")
        self.assertEqual((await stream.__anext__()).type, "retrieval")
        self.assertEqual((await stream.__anext__()).type, "tool_finished")
        first_answer = await asyncio.wait_for(stream.__anext__(), timeout=1)
        self.assertFalse(llm.release.is_set())
        self.assertIsNotNone(llm.calls[-1][1])

        llm.release.set()
        remaining = [event async for event in stream]
        self.assertEqual(first_answer.type, "answer_delta")
        self.assertEqual(first_answer.content, "第一段")
        self.assertEqual(
            [event.content for event in remaining],
            ["[[cite:1]]第二段"],
        )

    async def test_invalid_rag_result_is_reported_as_tool_failure(self):
        async def rag_search(query):
            return {"query": query, "chunks": ["invalid"]}

        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-rag",
                            "name": "rag_search",
                            "args": {"query": "测试"},
                            "type": "tool_call",
                        }
                    ],
                ),
                (["降级回答"], []),
            ]
        )
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("测试", [])]

        self.assertEqual(
            [event.type for event in events],
            ["tool_started", "tool_finished", "answer_delta"],
        )
        self.assertFalse(events[1].success)
        self.assertEqual(llm.calls[1][0][-1]["content"], "工具执行失败")

    async def test_empty_rag_result_still_emits_retrieval_event(self):
        async def rag_search(query):
            return {"query": query, "count": 0, "chunks": []}

        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-rag",
                            "name": "rag_search",
                            "args": {"query": "没有匹配的内容"},
                            "type": "tool_call",
                        }
                    ],
                ),
                (["未检索到相关资料"], []),
            ]
        )
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("测试", [])]

        self.assertEqual(events[1].type, "retrieval")
        self.assertEqual(events[1].data["result_count"], 0)
        self.assertEqual(events[1].data["chunks"], [])
        self.assertTrue(events[2].success)

    async def test_tool_failure_is_returned_to_model_for_recovery(self):
        async def database_query(question):
            del question
            raise ValueError("只允许执行只读查询")

        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-database",
                            "name": "database_query",
                            "args": {"question": "删除数据"},
                            "type": "tool_call",
                        }
                    ],
                ),
                (["无法执行该操作"], []),
            ]
        )
        tools = ToolExecutor()
        tools.register("database_query", "查询数据库", database_query)
        agent = AgentLoop(llm, tools)

        with self.assertLogs("agents.agent_loop", level="INFO") as captured:
            events = [
                event
                async for event in agent.run_stream(
                    "删除数据",
                    [],
                    run_id="run-test",
                )
            ]

        self.assertEqual(
            [event.type for event in events],
            ["tool_started", "tool_finished", "answer_delta"],
        )
        self.assertFalse(events[1].success)
        self.assertEqual(events[1].content, "工具执行失败")
        self.assertEqual(llm.calls[1][0][-1]["content"], "工具执行失败")
        log_output = "\n".join(captured.output)
        self.assertIn("run_id=run-test", log_output)
        self.assertIn("tool=database_query", log_output)
        self.assertIn("arguments={'question': '删除数据'}", log_output)
        self.assertIn("ValueError: 只允许执行只读查询", log_output)

    async def test_last_round_can_force_answer_without_tools(self):
        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-one",
                            "name": "Echo",
                            "args": {"value": "hello"},
                            "type": "tool_call",
                        }
                    ],
                ),
                (["最终", "回答"], []),
            ]
        )
        tools = ToolExecutor()
        tools.register("Echo", "回显", lambda value: value)
        agent = AgentLoop(llm, tools)
        agent.max_rounds = 2
        agent.force_answer = True

        events = [event async for event in agent.run_stream("测试", [])]

        self.assertEqual(
            [event.content for event in events if event.type == "answer_delta"],
            ["最终", "回答"],
        )
        self.assertIsNotNone(llm.calls[0][1])
        self.assertIsNone(llm.calls[1][1])
        self.assertIn("当前是第 1/2 轮", llm.calls[0][0][0]["content"])
        final_instruction = llm.calls[1][0][0]
        self.assertEqual(final_instruction["role"], "system")
        self.assertIn("当前是第 2/2 轮", final_instruction["content"])
        self.assertIn("第 2 轮只允许最终回答", final_instruction["content"])
        self.assertIn("依据已有结果给出最终回答", final_instruction["content"])
        self.assertIn("缺失数据如实说明", final_instruction["content"])
        self.assertIn("不得再承诺查询或调用工具", final_instruction["content"])
        self.assertNotIn("本轮工具已禁用", llm.calls[0][0][0]["content"])

    async def test_forced_final_answer_streams_citations_before_completion(self):
        class DelayedCitationLlm:
            model = "fake-model"

            def __init__(self):
                self.call_count = 0
                self.release = asyncio.Event()

            async def stream(self, messages, tools=None, temperature=0):
                del messages, temperature
                self.call_count += 1
                if self.call_count == 1:
                    yield "", [
                        {
                            "id": "call-rag",
                            "name": "rag_search",
                            "args": {"query": "测试引用"},
                            "type": "tool_call",
                        }
                    ]
                    return
                self.assert_no_tools(tools)
                yield "第一段<<SRC_", []
                await self.release.wait()
                yield "1>>第二段", []

            @staticmethod
            def assert_no_tools(tools):
                if tools is not None:
                    raise AssertionError("最终回答轮次不应绑定工具")

        async def rag_search(query):
            return {
                "query": query,
                "chunks": [
                    {
                        "text": "引用依据",
                        "source": "资料.pdf",
                        "score": 0.9,
                    }
                ],
            }

        llm = DelayedCitationLlm()
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        agent = AgentLoop(llm, tools)
        agent.max_rounds = 2
        agent.force_answer = True
        stream = agent.run_stream("测试流式引用", [])

        self.assertEqual((await stream.__anext__()).type, "tool_started")
        self.assertEqual((await stream.__anext__()).type, "retrieval")
        self.assertEqual((await stream.__anext__()).type, "tool_finished")
        first_answer = await asyncio.wait_for(stream.__anext__(), timeout=1)

        self.assertEqual(first_answer.content, "第一段")
        self.assertFalse(llm.release.is_set())
        llm.release.set()
        remaining = [event async for event in stream]
        self.assertEqual(
            [event.content for event in remaining],
            ["[[cite:1]]第二段"],
        )

    async def test_client_events_are_emitted_before_tool_finishes(self):
        llm = FakeLlm(
            [
                (
                    [],
                    [
                        {
                            "id": "call-observation",
                            "name": "render_result",
                            "args": {
                                "operation": "area",
                            },
                            "type": "tool_call",
                        }
                    ],
                ),
                (["查询完成"], []),
            ]
        )

        async def render_result(**arguments):
            return ToolOutput(
                content={"arguments": arguments, "total": 1},
                client_events=(
                    {"action_type": "CLEAR", "payload": {"layer_ids": ["stations"]}},
                    {"action_type": "MARKER_ADD", "payload": {"markers": []}},
                ),
            )

        tools = ToolExecutor()
        tools.register("render_result", "测试展示工具", render_result)
        agent = AgentLoop(llm, tools)

        events = [event async for event in agent.run_stream("显示站点", [])]

        self.assertEqual(
            [event.type for event in events],
            [
                "tool_started",
                "map_action",
                "map_action",
                "tool_finished",
                "answer_delta",
            ],
        )
        self.assertEqual(events[1].data["seq"], 1)
        self.assertEqual(events[2].data["seq"], 2)
        self.assertIn('"total": 1', llm.calls[1][0][-1]["content"])
