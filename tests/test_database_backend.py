import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from agents.tools.database_query import DatabaseQueryTool
from agents.tools.executor import ToolExecutor

from tool_backends.database_backend import (
    DatabaseConnectionError,
    DatabaseInformationCatalog,
    DatabaseQueryBackend,
    MysqlReadonlyGateway,
    ReadonlySqlValidator,
    SqlExecutionError,
    SqlGenerationError,
    SqlValidationError,
)


class FakeCatalog:
    def read_guide(self):
        return "guide"

    def read_all_schemas(self):
        return "schema"


class FakeLlm:
    def __init__(
        self,
        response="SELECT COUNT(*) FROM dwd_as_chemistry_observations_i_d",
        error=None,
    ):
        self.response = response
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages):
        del messages
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=self.response)


class DatabaseBackendErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_execution_error_survives_repair_and_tool_serialization(self):
        for code in (1054, 1105, 1142, 9999):
            with self.subTest(code=code):
                gateway = self.gateway()
                detail = "Unknown column\n" + "detail " * 100
                cursor = AsyncMock()
                cursor.execute.side_effect = [
                    value for _ in range(2)
                    for value in (None, Exception(code, detail))
                ]
                connection = MagicMock()
                connection.cursor.return_value.__aenter__ = AsyncMock(return_value=cursor)
                pool = MagicMock()
                pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
                gateway._get_pool = AsyncMock(return_value=pool)
                llm = FakeLlm()
                llm.ainvoke = AsyncMock(wraps=llm.ainvoke)
                backend = DatabaseQueryBackend(
                    catalog=FakeCatalog(), gateway=gateway, llm=llm,
                    generation_timeout=1,
                )
                executor = ToolExecutor()
                executor.register("database_query", "query", DatabaseQueryTool(backend).execute)
                result = json.loads(await executor.execute("database_query", {"question": "站位数量", "limit": 10}))
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], {
                    "type": "sql_execution", "code": code, "message": detail,
                })
                self.assertIn("LIMIT 11", result["sql"])
                self.assertEqual(llm.calls, 2)
                self.assertEqual(cursor.execute.await_count, 4)
                repair_prompt = llm.ainvoke.await_args.args[0][1]["content"]
                self.assertIn(detail, repair_prompt)
                self.assertIn(str(code), repair_prompt)
                self.assertIn(result["sql"], repair_prompt)

    async def test_tool_success_and_local_failure_contract(self):
        backend = SimpleNamespace(query=AsyncMock(return_value={
            "sql": "SELECT 1", "columns": ["value"], "rows": [], "truncated": False,
        }))
        tool = DatabaseQueryTool(backend)
        result = await tool.execute("查询")
        self.assertTrue(result["success"])
        self.assertEqual(result["rows"], [])
        for error in (SqlValidationError("只允许 SELECT"), DatabaseConnectionError("连接失败")):
            backend.query.side_effect = error
            result = await tool.execute("查询")
            self.assertEqual(result, {
                "success": False, "sql": None,
                "error": {"type": error.error_type, "code": None, "message": str(error)},
            })
        backend.query.side_effect = TypeError("programming bug")
        with self.assertRaises(TypeError):
            await tool.execute("查询")

    @staticmethod
    def gateway(connect_timeout=0.01):
        return MysqlReadonlyGateway(
            host="database.local",
            port=3306,
            user="reader",
            password="secret",
            database="donghai_dwd",
            connect_timeout=connect_timeout,
            query_timeout=1,
            configured_max_rows=50,
            allowed_tables=("dwd_as_chemistry_observations_i_d",),
            pool_max_size=1,
            ssl_enabled=False,
            ssl_ca=None,
            ssl_verify_cert=True,
        )

    async def test_connection_timeout_is_not_reported_as_query_timeout(self):
        received_options = {}

        async def create_pool(**options):
            received_options.update(options)
            await asyncio.sleep(1)

        asyncmy = SimpleNamespace(create_pool=create_pool)
        with patch.dict(sys.modules, {"asyncmy": asyncmy}):
            with self.assertRaisesRegex(
                DatabaseConnectionError,
                "数据库连接超时",
            ):
                await self.gateway()._get_pool()

        self.assertEqual(received_options["minsize"], 1)

    def test_catalog_authorizes_only_current_doris_observation_tables(self):
        project_root = Path(__file__).resolve().parents[1]
        catalog = DatabaseInformationCatalog(
            project_root / "database_information",
            guide_filename="数据库_AI_SQL查询指南.md",
        )

        self.assertEqual(
            catalog.available_tables,
            (
                "dwd_as_chemistry_observations_i_d",
                "dwd_as_hydromet_observations_i_d",
            ),
        )
        schemas = catalog.read_all_schemas()
        guide = catalog.read_guide()
        self.assertIn("ENGINE=OLAP", schemas)
        self.assertNotIn("section_stations", schemas)
        self.assertIn("station_name", guide)
        self.assertIn("JSON_OBJECT", guide)
        self.assertIn("组成业务唯一键", guide)
        self.assertIn("一条水文记录对应多条化学记录", guide)
        self.assertIn("REPLACE(UPPER(TRIM(station_name)), '-', '')", guide)
        self.assertIn("数据画像快照", guide)
        self.assertIn("COUNT(field)", guide)

    def test_sql_prompt_delegates_domain_rules_to_query_guide(self):
        prompt = DatabaseQueryBackend._SYSTEM_PROMPT

        self.assertIn("Apache Doris", prompt)
        self.assertIn("查询指南是业务语义和查询组织规则的唯一依据", prompt)
        self.assertIn("禁止 FULL OUTER JOIN", prompt)
        self.assertNotIn("JSON_OBJECT", prompt)

    def test_validator_accepts_new_tables_and_rejects_old_tables(self):
        validator = ReadonlySqlValidator()
        allowed_tables = (
            "dwd_as_chemistry_observations_i_d",
            "dwd_as_hydromet_observations_i_d",
        )

        sql = validator.validate(
            "SELECT AVG(temperature_c) AS avg_temperature_c "
            "FROM dwd_as_chemistry_observations_i_d "
            "WHERE UPPER(TRIM(station_name)) LIKE 'E2%'",
            50,
            allowed_tables,
        )

        self.assertIn("dwd_as_chemistry_observations_i_d", sql)
        self.assertIn("LIMIT 51", sql)
        with self.assertRaises(SqlValidationError):
            validator.validate(
                "SELECT * FROM section_stations",
                50,
                allowed_tables,
            )

    async def test_execute_uses_doris_timeout_without_transaction_wrapper(self):
        gateway = self.gateway()
        cursor = AsyncMock()
        cursor.fetchall.return_value = [(1,)]
        cursor.description = (("value",),)

        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        connection = MagicMock()
        connection.cursor.return_value = cursor_context

        connection_context = MagicMock()
        connection_context.__aenter__ = AsyncMock(return_value=connection)
        connection_context.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value = connection_context
        gateway._get_pool = AsyncMock(return_value=pool)

        result = await gateway._execute("SELECT 1", 10)

        self.assertEqual(
            cursor.execute.await_args_list,
            [call("SET query_timeout = 1"), call("SELECT 1")],
        )
        self.assertNotIn("START TRANSACTION", str(cursor.execute.await_args_list))
        self.assertNotIn("rollback", str(connection.mock_calls))
        self.assertEqual(result["rows"], [{"value": 1}])

    async def test_sql_generation_failure_is_wrapped_and_not_retried(self):
        llm = FakeLlm(error=RuntimeError("upstream 502"))
        gateway = SimpleNamespace(query=None, close=None)
        backend = DatabaseQueryBackend(
            catalog=FakeCatalog(),
            gateway=gateway,
            llm=llm,
            generation_timeout=1,
        )

        with self.assertRaisesRegex(SqlGenerationError, "SQL 生成服务不可用"):
            await backend.query("查询站位数量", 10)

        self.assertEqual(llm.calls, 1)

    async def test_database_connection_failure_does_not_regenerate_sql(self):
        llm = FakeLlm()

        class Gateway:
            async def query(self, sql, limit):
                del sql, limit
                raise DatabaseConnectionError("数据库连接超时")

        backend = DatabaseQueryBackend(
            catalog=FakeCatalog(),
            gateway=Gateway(),
            llm=llm,
            generation_timeout=1,
        )

        with self.assertRaises(DatabaseConnectionError):
            await backend.query("查询站位数量", 10)

        self.assertEqual(llm.calls, 1)

    async def test_repairable_sql_error_gets_one_model_retry(self):
        llm = FakeLlm()

        class Gateway:
            def __init__(self):
                self.calls = 0

            async def query(self, sql, limit):
                del sql, limit
                self.calls += 1
                if self.calls == 1:
                    raise SqlExecutionError("未知字段")
                return {"rows": []}

        gateway = Gateway()
        backend = DatabaseQueryBackend(
            catalog=FakeCatalog(),
            gateway=gateway,
            llm=llm,
            generation_timeout=1,
        )

        result = await backend.query("查询站位数量", 10)

        self.assertEqual(result, {"rows": []})
        self.assertEqual(llm.calls, 2)
        self.assertEqual(gateway.calls, 2)


if __name__ == "__main__":
    unittest.main()
