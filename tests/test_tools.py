import json
import unittest
from datetime import datetime

from agents.tools.current_server_time import TOOL_NAME, matches_current_server_time
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_tool_executor


class ToolExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_can_be_described_and_executed(self):
        executor = ToolExecutor()
        executor.register("Echo", "回显输入", lambda value: f"result:{value}")

        self.assertIn("Echo", executor.descriptions())
        self.assertEqual(await executor.execute("Echo", "hello"), "result:hello")

    async def test_unknown_tool_is_rejected(self):
        executor = ToolExecutor()

        with self.assertRaises(LookupError):
            await executor.execute("Missing", "value")

    async def test_current_server_time_is_registered_and_returns_timezone(self):
        executor = build_tool_executor()

        self.assertIn(TOOL_NAME, executor.descriptions())
        result = json.loads(await executor.execute(TOOL_NAME, ""))

        self.assertIsNotNone(datetime.fromisoformat(result["datetime"]).tzinfo)
        self.assertIn("timezone", result)
        self.assertRegex(result["utc_offset"], r"^[+-]\d{4}$")

    def test_current_time_questions_match_server_time_tool(self):
        self.assertTrue(matches_current_server_time("现在几点了"))
        self.assertTrue(matches_current_server_time("以你现在服务器的时区为准"))
        self.assertTrue(matches_current_server_time("查询当前服务器时间"))
        self.assertFalse(matches_current_server_time("介绍一下时间管理方法"))
