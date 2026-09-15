from __future__ import annotations

from typing import Any, Optional

from config.settings import get_settings
from services.synonym_service import expand_query_synonyms
from tool_backends.rag_backend import RagSearchBackend


TOOL_NAME = "rag_search"
TOOL_DESCRIPTION = (
    "检索内部知识库中的文档、制度、报告或说明，返回相关片段及来源。"
    "当问题需要非结构化文档依据时使用。"
)
TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": (
                "用于知识库召回的完整问题或检索语句，应保留关键主题、实体和时间条件，"
                "不得为空。"
            ),
        },
        "tags": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "一个用于优先检索的文档标签名称。",
            },
            "maxItems": 50,
            "description": (
                "可选的文档标签名称列表，优先检索指定标签；"
                "未检索到结果时，平台会在配置的授权范围内扩大检索。"
                "省略或传空数组时不指定优先标签。"
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


class RagSearchTool:
    """Adapt the RAG backend to the agent tool contract."""

    def __init__(self, backend: Optional[RagSearchBackend] = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> RagSearchBackend:
        if self._backend is None:
            settings = get_settings().tools.rag
            rerank = settings.rerank
            self._backend = RagSearchBackend(
                recall_url=settings.resolved_recall_url(),
                api_token=settings.api_token.get_secret_value(),
                tenant_id=settings.tenant_id,
                knowledge_ids=tuple(settings.knowledge_ids),
                dept_ids=tuple(settings.dept_ids),
                score=settings.score,
                candidate_top_n=(
                    rerank.candidate_top_n if rerank.enabled else settings.top_n
                ),
                faq_score=settings.faq_score,
                search_method=settings.search_method,
                search_scope=settings.search_scope,
                timeout=settings.timeout,
                connect_timeout=settings.connect_timeout,
                connection_limit=settings.connection_limit,
                verify_ssl=settings.verify_ssl,
                max_retries=settings.max_retries,
                retry_backoff=settings.retry_backoff,
                rerank_enabled=rerank.enabled,
                rerank_url=settings.resolved_rerank_url(),
                rerank_model=rerank.model,
                rerank_timeout=rerank.timeout,
                rerank_connect_timeout=rerank.connect_timeout,
                rerank_max_retries=rerank.max_retries,
                rerank_retry_backoff=rerank.retry_backoff,
                rerank_verify_ssl=rerank.verify_ssl,
            )
        return self._backend

    async def execute(
        self,
        query: str,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query 不能为空")
        if tags is not None and (
            not isinstance(tags, list)
            or len(tags) > 50
            or any(not isinstance(tag, str) for tag in tags)
        ):
            raise ValueError("tags 必须是不超过 50 项的字符串数组")
        query = expand_query_synonyms(query)
        chunks = await self.backend.search(
            query, get_settings().tools.rag.top_n, {"tags": tags or []}
        )
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
                    "page_index": chunk.page_index,
                    "line": chunk.line,
                    "page": chunk.page,
                }
                for chunk in chunks
            ],
        }

    async def close(self) -> None:
        if self._backend is not None:
            await self._backend.close()
            self._backend = None
