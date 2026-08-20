import asyncio
import unittest
from copy import deepcopy

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_tool_executor
from schemas.chat import ChatMessage


class FakeLlm:
    model = "fake-model"

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def stream(self, messages, tools=None, temperature=0):
        del temperature
        self.calls.append((deepcopy(messages), tools))
        content_parts, tool_calls = next(self.responses)
        for content in content_parts:
            yield content, []
        if tool_calls:
            yield "", tool_calls


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_discards_pre_tool_text_instead_of_streaming_it_as_answer(self):
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
            "查询完成",
        )
        self.assertEqual(
            [event.type for event in events],
            ["tool_started", "tool_finished", "answer_delta"],
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
        self.assertEqual(events[1].sources[0]["citation_id"], 1)
        self.assertEqual(
            "".join(event.content for event in events if event.type == "answer_delta"),
            "综合回答[[cite:1]]",
        )
        react_messages = llm.calls[-2][0]
        self.assertIn("报告依据", react_messages[-3]["content"])
        self.assertIn('"source_key": "<<SRC_1>>"', react_messages[-3]["content"])
        self.assertIn('"avg": 30.1', react_messages[-1]["content"])
        merger_messages, merger_tools = llm.calls[-1]
        self.assertIsNone(merger_tools)
        self.assertIn("候选回答", merger_messages[-1]["content"])
        self.assertIn("来源键：<<SRC_1>>", merger_messages[-1]["content"])

    async def test_streams_first_answer_chunk_before_final_model_chunk(self):
        class DelayedLlm:
            model = "fake-model"

            def __init__(self):
                self.call_count = 0
                self.release = asyncio.Event()

            async def stream(self, messages, tools=None, temperature=0):
                del messages, tools, temperature
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
        first_answer = await asyncio.wait_for(stream.__anext__(), timeout=0.2)

        self.assertEqual(first_answer.type, "answer_delta")
        self.assertEqual(first_answer.content, "第一段")
        self.assertFalse(llm.release.is_set())

        llm.release.set()
        remaining = [event async for event in stream]
        self.assertEqual([event.content for event in remaining], ["第二段"])

    async def test_streams_citation_before_final_merger_finishes(self):
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
                if self.call_count == 2:
                    yield "候选回答<<SRC_1>>", []
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
        first_answer = await asyncio.wait_for(stream.__anext__(), timeout=0.2)

        self.assertEqual(first_answer.type, "answer_delta")
        self.assertEqual(first_answer.content, "第一段")
        self.assertFalse(llm.release.is_set())
        self.assertIsNone(llm.calls[-1][1])

        llm.release.set()
        remaining = [event async for event in stream]
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

        events = [event async for event in agent.run_stream("删除数据", [])]

        self.assertEqual(
            [event.type for event in events],
            ["tool_started", "tool_finished", "answer_delta"],
        )
        self.assertFalse(events[1].success)
        self.assertEqual(events[1].content, "工具执行失败")
        self.assertEqual(llm.calls[1][0][-1]["content"], "工具执行失败")

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
                (["最终回答"], []),
            ]
        )
        tools = ToolExecutor()
        tools.register("Echo", "回显", lambda value: value)
        agent = AgentLoop(llm, tools)
        agent.max_rounds = 2
        agent.force_answer = True

        events = [event async for event in agent.run_stream("测试", [])]

        self.assertEqual(events[-1].content, "最终回答")
        self.assertIsNotNone(llm.calls[0][1])
        self.assertIsNone(llm.calls[1][1])
