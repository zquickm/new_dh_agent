from __future__ import annotations

from typing import Any, Optional

from tool_backends.rag_backend import RagSearchBackend


TOOL_NAME = "rag_search"
TOOL_DESCRIPTION = (
    "检索内部知识库中的文档、制度、报告或说明，返回相关片段及来源。"
    "当问题需要非结构化文档依据时使用。"
)
TOOL_PARAMETERS: dict[str, Any] = {
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
}


class RagSearchTool:
    """Adapt the RAG backend to the ReAct tool contract."""

    def __init__(self, backend: Optional[RagSearchBackend] = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> RagSearchBackend:
        if self._backend is None:
            self._backend = RagSearchBackend.from_env()
        return self._backend

    async def execute(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query 不能为空")
        top_k = min(max(int(top_k), 1), 20)
        chunks = await self.backend.search(query, top_k, filters or {})
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

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
