from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config.settings import get_settings
from tool_backends.database_backend import (
    DatabaseInformationCatalog,
    DatabaseQueryBackend,
    DatabaseToolError,
    MysqlReadonlyGateway,
)


TOOL_NAME = "database_query"
TOOL_DESCRIPTION = (
    "将自然语言问题转换为 Apache Doris（MySQL 兼容方言）只读 SQL，查询授权业务表并返回结构化结果。"
    "每次查询最多返回 {max_rows} 行，超出截断。"
    "不得用于地图选区得到的业务观测站，也不得用于明确站名的业务观测站状态、详情、"
    "最新观测、趋势或时间区间数据；这些问题必须调用 spatial_query_service。"
)
def build_tool_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "需要从授权业务表回答的完整自然语言问题，应保留用户指定的过滤条件、"
                    "指标、排序、分组和统计要求，不得为空。"
                ),
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    }


class DatabaseQueryTool:
    """Adapt the database backend to the agent tool contract."""

    def __init__(self, backend: Optional[DatabaseQueryBackend] = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> DatabaseQueryBackend:
        if self._backend is None:
            try:
                from langchain_openai import ChatOpenAI
            except ImportError as exc:
                raise DatabaseToolError("缺少 langchain-openai 依赖") from exc

            settings = get_settings()
            database = settings.tools.database
            sql_llm = database.sql_llm
            information_dir = Path(database.information_dir)
            if not information_dir.is_absolute():
                information_dir = Path(__file__).resolve().parents[2] / information_dir
            catalog = DatabaseInformationCatalog(
                information_dir,
                guide_filename=database.query_guide_file,
            )
            gateway = MysqlReadonlyGateway(
                host=database.host,
                port=database.port,
                user=database.user,
                password=database.password.get_secret_value(),
                database=database.name,
                connect_timeout=database.connect_timeout,
                query_timeout=database.query_timeout,
                configured_max_rows=database.max_rows,
                allowed_tables=catalog.available_tables,
                pool_max_size=database.pool_max_size,
                ssl_enabled=database.ssl_enabled,
                ssl_ca=database.ssl_ca,
                ssl_verify_cert=database.ssl_verify_cert,
            )
            llm = ChatOpenAI(
                api_key=(
                    sql_llm.api_key.get_secret_value()
                    or settings.llm.api_key.get_secret_value()
                    or "not-required"
                ),
                base_url=sql_llm.base_url or settings.llm.base_url,
                model=sql_llm.model or settings.llm.model,
                temperature=sql_llm.temperature,
                timeout=sql_llm.timeout,
                max_retries=1,
            )
            self._backend = DatabaseQueryBackend(
                catalog=catalog,
                gateway=gateway,
                llm=llm,
                generation_timeout=sql_llm.timeout,
            )
        return self._backend

    async def execute(
        self,
        question: str,
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise ValueError("question 不能为空")
        max_rows = get_settings().tools.database.max_rows
        try:
            result = await self.backend.query(question, max_rows)
        except DatabaseToolError as exc:
            return {
                "success": False,
                "sql": exc.sql,
                "error": {
                    "type": exc.error_type,
                    "code": exc.code,
                    "message": str(exc),
                },
            }
        return {**result, "success": True}

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
