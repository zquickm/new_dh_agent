import json
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace

from agents.react_agent import ActionAgent
from agents.tools.current_server_time import TOOL_NAME, matches_current_server_time
from agents.tools.database_query import DatabaseQueryTool
from agents.tools.executor import ToolExecutor
from agents.tools.rag_search import RagSearchTool
from agents.tools.registry import build_tool_executor


class ToolExecutorTest(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_can_be_described_and_executed(self):
        executor = ToolExecutor()
        executor.register("Echo", "回显输入", lambda value: f"result:{value}")

        self.assertIn("Echo", executor.descriptions())
        self.assertEqual(await executor.execute("Echo", "hello"), "result:hello")

    async def test_async_tool_accepts_json_object_arguments(self):
        executor = ToolExecutor()

        async def combine(query, top_k=5):
            return {"query": query, "top_k": top_k}

        executor.register("Search", "搜索", combine)
        result = await executor.execute(
            "Search", '{"query":"东海","top_k":3}'
        )

        self.assertEqual(json.loads(result), {"query": "东海", "top_k": 3})

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

    async def test_rag_and_database_adapters_serialize_backend_results(self):
        class FakeRagBackend:
            async def search(self, query, top_k, filters):
                self.arguments = (query, top_k, filters)
                return [
                    SimpleNamespace(
                        text="片段",
                        source="文档",
                        score=0.9,
                        document_id="doc-1",
                        chunk_id="chunk-1",
                        knowledge_id="kb-1",
                        title="标题",
                        url=None,
                        page=2,
                    )
                ]

            async def close(self):
                pass

        class FakeDatabaseBackend:
            async def query(self, question, limit):
                return {"question": question, "limit": limit, "rows": []}

            async def close(self):
                pass

        rag_tool = RagSearchTool(FakeRagBackend())
        database_tool = DatabaseQueryTool(FakeDatabaseBackend())

        rag_result = await rag_tool.execute("  赤潮  ", 30)
        database_result = await database_tool.execute("  E2 平均盐度  ", 2000)

        self.assertEqual(rag_result["count"], 1)
        self.assertEqual(rag_result["chunks"][0]["source"], "文档")
        self.assertEqual(database_result["question"], "E2 平均盐度")
        self.assertEqual(database_result["limit"], 1000)

    async def test_action_agent_parses_json_and_calls_async_tool(self):
        class FakeLlm:
            async def invoke(self, messages, temperature=0):
                del messages, temperature
                return 'rag_search[{"query":"东海赤潮","top_k":3}]'

        async def rag_search(query, top_k=5):
            return {"query": query, "top_k": top_k}

        executor = ToolExecutor()
        executor.register("rag_search", "检索知识库", rag_search)

        result = await ActionAgent(FakeLlm(), executor).run(["user: 查询赤潮"])

        self.assertTrue(result.success)
        self.assertEqual(result.tool_name, "rag_search")
        self.assertIn('"top_k": 3', result.observation)

    async def test_action_agent_prints_serious_backend_error_to_stdout(self):
        class FakeLlm:
            async def invoke(self, messages, temperature=0):
                del messages, temperature
                return 'rag_search[{"query":"东海赤潮"}]'

        async def rag_search(query):
            del query
            try:
                raise ConnectionError("upstream disconnected")
            except ConnectionError as exc:
                raise RuntimeError("RAG service request failed") from exc

        executor = ToolExecutor()
        executor.register("rag_search", "检索知识库", rag_search)
        output = io.StringIO()

        with redirect_stdout(output):
            result = await ActionAgent(FakeLlm(), executor).run(["user: 查询赤潮"])

        self.assertFalse(result.success)
        self.assertEqual(result.observation, "工具执行失败")
        self.assertIn("Serious error while executing tool rag_search", output.getvalue())
        self.assertIn("ConnectionError: upstream disconnected", output.getvalue())
        self.assertIn("RuntimeError: RAG service request failed", output.getvalue())

    async def test_action_agent_does_not_print_business_error(self):
        class FakeLlm:
            async def invoke(self, messages, temperature=0):
                del messages, temperature
                return 'database_query[{"question":"删除数据"}]'

        async def database_query(question):
            del question
            raise ValueError("只允许执行只读查询")

        executor = ToolExecutor()
        executor.register("database_query", "查询数据库", database_query)
        output = io.StringIO()

        with redirect_stdout(output):
            result = await ActionAgent(FakeLlm(), executor).run(["user: 删除数据"])

        self.assertFalse(result.success)
        self.assertEqual(output.getvalue(), "")

    async def test_registry_contains_all_three_tools_without_eager_backends(self):
        executor = build_tool_executor()

        self.assertEqual(
            set(executor.tools),
            {"CurrentServerTime", "rag_search", "database_query"},
        )
        await executor.close()
