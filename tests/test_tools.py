import unittest
from types import SimpleNamespace

from agents.react_agent import ActionAgent
from agents.tools import ApplicationTools, ToolExecutor


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

    async def test_async_tool_accepts_json_object_arguments(self):
        executor = ToolExecutor()

        async def combine(query, top_k=5):
            return {"query": query, "top_k": top_k}

        executor.register("Search", "搜索", combine)
        result = await executor.execute(
            "Search", '{"query":"东海","top_k":3}'
        )

        self.assertEqual(result, '{"query": "东海", "top_k": 3}')

    async def test_application_tools_serialize_backend_results(self):
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

        tools = ApplicationTools(FakeRagBackend(), FakeDatabaseBackend())

        rag_result = await tools.rag_search("  赤潮  ", 30)
        database_result = await tools.database_query("  E2 平均盐度  ", 2000)

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
