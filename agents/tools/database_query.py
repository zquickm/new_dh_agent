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
    "查询授权业务表中的断面历史调查数据，用于 E1/E2/E3 等断面在指定调查日期、"
    "采样深度下的水文气象、海水化学记录及跨记录统计。不得用于地图选区得到的观测站，"
    "也不得用于明确站名的站点状态、详情、最新观测、趋势或时间区间数据；这些问题必须调用 "
    "observation_service。SQL 会经过白名单和只读校验。"
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "minLength": 1},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 1000,
            "default": 100,
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

    async def execute(self, question: str, limit: int = 100) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise ValueError("question 不能为空")
        limit = min(max(int(limit), 1), 1000)
        return await self.backend.query(question, limit)

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
