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
            (["综合回答"], []),
        ]

        async def rag_search(query, top_k=5):
            return {"query": query, "top_k": top_k, "chunks": ["报告依据"]}

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
                "tool_finished",
                "tool_started",
                "tool_finished",
                "answer_delta",
            ],
        )
        self.assertEqual(
            [events[1].tool_name, events[3].tool_name],
            ["rag_search", "database_query"],
        )
        final_messages = llm.calls[-1][0]
        self.assertIn("报告依据", final_messages[-3]["content"])
        self.assertIn('"avg": 30.1', final_messages[-1]["content"])

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
