import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tool_backends.database_backend import (
    DatabaseConnectionError,
    DatabaseQueryBackend,
    MysqlReadonlyGateway,
    SqlExecutionError,
    SqlGenerationError,
)


class FakeCatalog:
    def read_guide(self):
        return "guide"

    def read_all_schemas(self):
        return "schema"


class FakeLlm:
    def __init__(self, response="SELECT COUNT(*) FROM section_stations", error=None):
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
    @staticmethod
    def gateway(connect_timeout=0.01):
        return MysqlReadonlyGateway(
            host="database.local",
            port=3306,
            user="reader",
            password="secret",
            database="document",
            connect_timeout=connect_timeout,
            query_timeout=1,
            configured_max_rows=50,
            allowed_tables=("section_stations",),
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
