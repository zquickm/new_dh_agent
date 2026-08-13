import unittest

from agents.tools import ToolExecutor


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
