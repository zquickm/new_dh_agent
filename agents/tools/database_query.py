from __future__ import annotations

from typing import Any, Optional

from config.settings import get_settings
from tool_backends.database_backend import DatabaseQueryBackend


TOOL_NAME = "database_query"
TOOL_DESCRIPTION = (
    "根据完整的自然语言问题查询只读业务数据库，用于精确数据、统计、"
    "站点或观测记录。SQL 会经过白名单和只读校验。"
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
    """Adapt the database backend to the ReAct tool contract."""

    def __init__(self, backend: Optional[DatabaseQueryBackend] = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> DatabaseQueryBackend:
        if self._backend is None:
            llm = get_settings().llm
            self._backend = DatabaseQueryBackend.from_env(
                default_llm_base_url=llm.base_url,
                default_llm_model=llm.model,
                default_llm_api_key=llm.api_key.get_secret_value(),
                default_llm_temperature=llm.temperature,
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
