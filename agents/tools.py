from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from config.settings import get_settings
from tool_backends.database_backend import DatabaseQueryBackend
from tool_backends.rag_backend import RagSearchBackend


ToolFunction = Callable[..., Any]


@dataclass(frozen=True)
class RegisteredTool:
    description: str
    function: ToolFunction
    parameters: dict[str, Any]


class ApplicationTools:
    """Thin adapters that expose backend results to the ReAct agent."""

    def __init__(
        self,
        rag_backend: RagSearchBackend,
        database_backend: DatabaseQueryBackend,
    ) -> None:
        self.rag_backend = rag_backend
        self.database_backend = database_backend

    async def rag_search(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query 不能为空")
        top_k = min(max(int(top_k), 1), 20)
        chunks = await self.rag_backend.search(query, top_k, filters or {})
        return {
            "query": query,
            "count": len(chunks),
            "chunks": [
                {
                    "text": chunk.text,
                    "source": chunk.source,
                    "score": chunk.score,
                    "document_id": chunk.document_id,
                    "chunk_id": chunk.chunk_id,
                    "knowledge_id": chunk.knowledge_id,
                    "title": chunk.title,
                    "url": chunk.url,
                    "page": chunk.page,
                }
                for chunk in chunks
            ],
        }

    async def database_query(
        self,
        question: str,
        limit: int = 100,
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise ValueError("question 不能为空")
        limit = min(max(int(limit), 1), 1000)
        return await self.database_backend.query(question, limit)

    async def close(self) -> None:
        await asyncio.gather(
            self.rag_backend.close(),
            self.database_backend.close(),
            return_exceptions=True,
        )


class ToolExecutor:
    """Register and execute synchronous or asynchronous ReAct tools."""

    def __init__(self, execution_timeout: float = 300.0) -> None:
        self.tools: dict[str, RegisteredTool] = {}
        self.execution_timeout = execution_timeout
        self._closers: list[Callable[..., Any]] = []

    def register(
        self,
        name: str,
        description: str,
        function: ToolFunction,
        parameters: Optional[dict[str, Any]] = None,
    ) -> None:
        self.tools[name] = RegisteredTool(
            description,
            function,
            parameters
            or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )

    def register_closer(self, closer: Callable[..., Any]) -> None:
        self._closers.append(closer)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self.tools.get(name)

    def descriptions(self) -> str:
        return json.dumps(self.definitions(), ensure_ascii=False, indent=2)

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for name, tool in self.tools.items()
        ]

    async def execute(self, name: str, argument: Any) -> str:
        tool = self.get(name)
        if tool is None:
            raise LookupError(f"Unknown tool: {name}")

        if isinstance(argument, str) and argument.lstrip().startswith("{"):
            try:
                parsed = json.loads(argument)
                if isinstance(parsed, dict):
                    argument = parsed
            except json.JSONDecodeError:
                pass

        args: tuple[Any, ...] = () if isinstance(argument, dict) else (argument,)
        kwargs = argument if isinstance(argument, dict) else {}

        async def invoke() -> Any:
            if inspect.iscoroutinefunction(tool.function):
                return await tool.function(*args, **kwargs)
            result = await asyncio.to_thread(tool.function, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        result = await asyncio.wait_for(invoke(), timeout=self.execution_timeout)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    async def close(self) -> None:
        for closer in reversed(self._closers):
            result = closer()
            if inspect.isawaitable(result):
                await result


def build_tool_executor() -> ToolExecutor:
    """Build the two application tools and their reusable backend resources."""

    project_dir = Path(__file__).resolve().parent.parent
    load_dotenv(project_dir / ".env")

    llm_settings = get_settings().llm
    rag_backend = RagSearchBackend.from_env()
    database_backend = DatabaseQueryBackend.from_env(
        default_llm_base_url=llm_settings.base_url,
        default_llm_model=llm_settings.model,
        default_llm_api_key=llm_settings.api_key.get_secret_value(),
        default_llm_temperature=llm_settings.temperature,
    )
    application_tools = ApplicationTools(rag_backend, database_backend)
    executor = ToolExecutor()
    executor.register(
        "rag_search",
        (
            "检索内部知识库中的文档、制度、报告或说明，返回相关片段及来源。"
            "当问题需要非结构化文档依据时使用。"
        ),
        application_tools.rag_search,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
                "filters": {
                    "type": "object",
                    "properties": {
                        "knowledge_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "dept_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    executor.register(
        "database_query",
        (
            "根据完整的自然语言问题查询只读业务数据库，用于精确数据、统计、"
            "站点或观测记录。SQL 会经过白名单和只读校验。"
        ),
        application_tools.database_query,
        {
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
        },
    )
    executor.register_closer(application_tools.close)
    return executor
