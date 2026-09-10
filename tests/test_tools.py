import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from agents.tools.current_server_time import TOOL_NAME, matches_current_server_time
from agents.tools.database_query import (
    TOOL_DESCRIPTION as DATABASE_TOOL_DESCRIPTION,
    DatabaseQueryTool,
)
from agents.tools.executor import ToolExecutor, ToolOutput
from agents.tools.rag_search import RagSearchTool
from services.synonym_service import clear_synonym_matcher_cache, expand_query_synonyms
from agents.tools.registry import build_tool_executor
from config.settings import get_settings
from services.dictionary_service import dictionary_store
from tool_backends.rag_backend import RagSearchBackend


class ToolExecutorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.synonym_patcher = patch.object(
            dictionary_store,
            "synonym_groups",
            return_value=[
                {
                    "standardTerm": "溶解氧",
                    "terms": [{"term": "DO"}, {"term": "溶氧"}],
                }
            ],
        )
        self.synonym_patcher.start()
        clear_synonym_matcher_cache()

    def tearDown(self):
        clear_synonym_matcher_cache()
        self.synonym_patcher.stop()

    def test_database_description_is_generic_and_excludes_observation_stations(self):
        self.assertIn("授权业务表", DATABASE_TOOL_DESCRIPTION)
        self.assertIn("Apache Doris", DATABASE_TOOL_DESCRIPTION)
        self.assertIn("必须调用 observation_service", DATABASE_TOOL_DESCRIPTION)
        self.assertNotIn("断面", DATABASE_TOOL_DESCRIPTION)

    async def test_database_schema_limit_matches_server_max_rows(self):
        executor = build_tool_executor()
        schema = executor.get("database_query").parameters["properties"]["limit"]
        max_rows = get_settings().tools.database.max_rows

        self.assertEqual(schema["maximum"], max_rows)
        self.assertEqual(schema["default"], max_rows)
        await executor.close()

    async def test_registered_tool_can_be_described_and_executed(self):
        executor = ToolExecutor()
        executor.register("Echo", "回显输入", lambda value: f"result:{value}")

        self.assertIn("Echo", executor.descriptions())
        self.assertEqual(await executor.execute("Echo", "hello"), "result:hello")

    def test_definitions_use_openai_tool_schema(self):
        executor = ToolExecutor()
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        }
        executor.register("Search", "搜索资料", lambda query: query, parameters)

        self.assertEqual(
            executor.definitions(),
            [
                {
                    "type": "function",
                    "function": {
                        "name": "Search",
                        "description": "搜索资料",
                        "parameters": parameters,
                    },
                }
            ],
        )

    def test_registered_tool_parameter_nodes_have_descriptions(self):
        missing = []

        def inspect_schema(schema, path):
            for name, node in schema.get("properties", {}).items():
                node_path = f"{path}.{name}" if path else name
                if not str(node.get("description") or "").strip():
                    missing.append(node_path)
                inspect_schema(node, node_path)

                items = node.get("items")
                if isinstance(items, dict):
                    item_path = f"{node_path}[]"
                    if not str(items.get("description") or "").strip():
                        missing.append(item_path)
                    inspect_schema(items, item_path)

        executor = build_tool_executor()
        for definition in executor.definitions():
            function = definition["function"]
            inspect_schema(function["parameters"], function["name"])

        self.assertEqual(missing, [])

    async def test_no_argument_tool_accepts_native_empty_arguments(self):
        executor = build_tool_executor()

        result = json.loads(await executor.execute(TOOL_NAME, {}))

        self.assertIn("datetime", result)
        await executor.close()

    async def test_async_tool_accepts_json_object_arguments(self):
        executor = ToolExecutor()

        async def combine(query, top_k=5):
            return {"query": query, "top_k": top_k}

        executor.register("Search", "搜索", combine)
        result = await executor.execute(
            "Search", '{"query":"东海","top_k":3}'
        )

        self.assertEqual(json.loads(result), {"query": "东海", "top_k": 3})

    async def test_tool_output_separates_model_content_and_client_events(self):
        executor = ToolExecutor()

        async def spatial_result():
            return ToolOutput(
                content={"total": 1},
                client_events=(
                    {"action_type": "LOCATE", "payload": {"points": [[1, 2]]}},
                ),
            )

        executor.register("Spatial", "空间查询", spatial_result)

        content, events = await executor.execute_with_events("Spatial", {})

        self.assertEqual(json.loads(content), {"total": 1})
        self.assertEqual(events[0]["action_type"], "LOCATE")

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
                        page_index=1,
                        line=None,
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
        self.assertEqual(rag_result["chunks"][0]["page_index"], 1)
        self.assertEqual(rag_result["chunks"][0]["page"], 2)
        self.assertEqual(rag_tool.backend.arguments, ("赤潮", 20, {}))
        self.assertEqual(database_result["question"], "E2 平均盐度")
        self.assertEqual(
            database_result["limit"],
            get_settings().tools.database.max_rows,
        )

    async def test_rag_expands_synonyms_before_search(self):
        class FakeRagBackend:
            async def search(self, query, top_k, filters):
                self.query = query
                return []

        rag_tool = RagSearchTool(FakeRagBackend())
        result = await rag_tool.execute("请分析7月DO的达标率")

        self.assertEqual(rag_tool.backend.query, "请分析7月DO（溶解氧）的达标率")
        self.assertEqual(result["query"], "请分析7月DO（溶解氧）的达标率")
        self.assertEqual(
            expand_query_synonyms("DO（溶解氧）和pH值"),
            "DO（溶解氧）和pH值",
        )

    def test_rag_parser_preserves_page_index_and_line(self):
        chunks = RagSearchBackend._parse_recall(
            {
                "code": 200,
                "data": {
                    "records": [
                        {
                            "chunk_text": "片段正文",
                            "fileName": "资料.pdf",
                            "score": 0.8,
                            "page_index": 2,
                            "line": 0,
                        }
                    ]
                },
            },
            5,
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].page_index, 2)
        self.assertEqual(chunks[0].line, 0)
        self.assertEqual(chunks[0].page, 3)

    def test_rag_parser_matches_backend_line_fallback(self):
        chunks = RagSearchBackend._parse_recall(
            {
                "code": 200,
                "data": {
                    "records": [
                        {
                            "chunk_text": "片段正文",
                            "fileName": "资料.pdf",
                            "page_index": 2,
                        }
                    ]
                },
            },
            5,
        )

        self.assertEqual(chunks[0].page_index, 2)
        self.assertEqual(chunks[0].line, 3)
        self.assertEqual(chunks[0].page, 3)

    async def test_registry_contains_all_tools_without_eager_backends(self):
        executor = build_tool_executor()

        self.assertEqual(
            set(executor.tools),
            {
                "load_skill",
                "CurrentServerTime",
                "rag_search",
                "database_query",
                "observation_service",
                "map_action",
                "dm_duanmian_analysis",
            },
        )
        await executor.close()
