import unittest

from agents.react_agent import CoreAgent
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
            ["action_started", "action_finished", "answer"],
        )
        self.assertEqual(events[1].tool_name, "CurrentServerTime")
        self.assertTrue(events[1].success)
        self.assertIn('"datetime"', events[1].content)
        self.assertIn("CurrentServerTime返回", llm.stream_prompts[0])
        self.assertEqual(llm.invoke_calls, 0)

    async def test_route_prompt_contains_available_tool_descriptions(self):
        llm = FakeLlm()
        agent = CoreAgent(llm, build_tool_executor())

        self.assertEqual(await agent.route(["user: 查询实时信息"], 1), "answer")

        self.assertEqual(llm.invoke_calls, 1)
        self.assertIn("CurrentServerTime", llm.invoke_prompts[0])
        self.assertIn("获取当前服务器", llm.invoke_prompts[0])
