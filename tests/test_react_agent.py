import unittest

from agents.react_agent import CoreAgent
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_tool_executor


class FakeLlm:
    model = "fake-model"

    def __init__(self):
        self.invoke_calls = 0
        self.invoke_prompts = []
        self.stream_prompts = []

    async def invoke(self, messages, temperature=None):
        del temperature
        self.invoke_calls += 1
        self.invoke_prompts.append(messages[-1]["content"])
        return '{"agent":"answer"}'

    async def stream(self, messages):
        self.stream_prompts.append(messages[-1]["content"])
        yield "当前服务器时间已获取"


class ReActAgentTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_time_question_executes_tool_before_answer(self):
        llm = FakeLlm()
        agent = CoreAgent(llm, build_tool_executor())

        events = [event async for event in agent.run_stream("现在几点了", [])]

        self.assertEqual(
            [event.type for event in events],
            [
                "action_started",
                "action_finished",
                "think",
                "think_finished",
                "answer",
            ],
        )
        self.assertEqual(events[1].tool_name, "CurrentServerTime")
        self.assertTrue(events[1].success)
        self.assertIn('"datetime"', events[1].content)
        self.assertIn("CurrentServerTime返回", llm.stream_prompts[-1])
        self.assertEqual(llm.invoke_calls, 1)

    async def test_route_prompt_contains_available_tool_descriptions(self):
        llm = FakeLlm()
        agent = CoreAgent(llm, build_tool_executor())

        self.assertEqual(await agent.route(["user: 查询实时信息"], 1), "answer")

        self.assertEqual(llm.invoke_calls, 1)
        self.assertIn("CurrentServerTime", llm.invoke_prompts[0])
        self.assertIn("获取当前服务器", llm.invoke_prompts[0])
        self.assertIn("实际观测值", llm.invoke_prompts[0])
        self.assertIn("内部文档", llm.invoke_prompts[0])

    async def test_complex_task_can_call_rag_then_database_before_answer(self):
        class MultiToolLlm:
            model = "fake-model"

            def __init__(self):
                self.responses = iter(
                    [
                        '{"agent":"action"}',
                        'rag_search[{"query":"E2断面相关报告","top_k":5}]',
                        '{"agent":"action"}',
                        'database_query[{"question":"查询E2断面盐度平均值","limit":100}]',
                    ]
                )
                self.answer_prompt = ""

            async def invoke(self, messages, temperature=None):
                del messages, temperature
                return next(self.responses)

            async def stream(self, messages):
                self.answer_prompt = messages[-1]["content"]
                yield "综合回答"

        async def rag_search(query, top_k=5):
            return {"query": query, "top_k": top_k, "chunks": ["报告依据"]}

        async def database_query(question, limit=100):
            return {"question": question, "limit": limit, "rows": [{"avg": 30.1}]}

        llm = MultiToolLlm()
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        tools.register("database_query", "查询实际数据", database_query)
        agent = CoreAgent(llm, tools)

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
                "action_started",
                "action_finished",
                "think",
                "think_finished",
                "action_started",
                "action_finished",
                "think",
                "think_finished",
                "answer",
            ],
        )
        self.assertEqual(
            [events[1].tool_name, events[5].tool_name],
            ["rag_search", "database_query"],
        )
        self.assertIn("报告依据", llm.answer_prompt)
        self.assertIn('"avg": 30.1', llm.answer_prompt)

    async def test_think_cannot_run_twice_in_a_row(self):
        class RepeatingThinkLlm(FakeLlm):
            async def invoke(self, messages, temperature=None):
                del temperature
                self.invoke_calls += 1
                self.invoke_prompts.append(messages[-1]["content"])
                return '{"agent":"think"}'

        llm = RepeatingThinkLlm()
        agent = CoreAgent(llm, ToolExecutor())

        events = [event async for event in agent.run_stream("分析一下", [])]

        self.assertEqual(
            [event.type for event in events],
            ["think", "think_finished", "answer"],
        )
        self.assertIn("可选步骤：answer", llm.invoke_prompts[1])
        self.assertNotIn("可选步骤：answer, think", llm.invoke_prompts[1])
