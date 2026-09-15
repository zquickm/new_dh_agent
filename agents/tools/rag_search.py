from __future__ import annotations

import asyncio

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
        self._backends: dict[str, RagSearchBackend] = {}

    def _build_backend(self, top_n: int, knowledge_id: str) -> RagSearchBackend:
        settings = get_settings().tools.rag
        rerank = settings.rerank
        return RagSearchBackend(
            recall_url=settings.resolved_recall_url(),
            api_token=settings.api_token.get_secret_value().strip(),
            tenant_id=settings.tenant_id,
            knowledge_ids=(knowledge_id,),
            dept_ids=tuple(settings.dept_ids),
            score=settings.score,
            candidate_top_n=top_n,
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
        settings = get_settings().tools.rag
        if self._backend is not None:
            chunks = await self._backend.search(
                query, sum(item.top_n for item in settings.libraries.values()), {"tags": tags or []}
            )
        else:
            libraries = settings.libraries
            if len(libraries) != 3 or any(
                not item.knowledge_id.strip() for item in libraries.values()
            ):
                raise ValueError("RAG 必须配置三个知识库及各自的 knowledge_id")
            if len({item.knowledge_id.strip() for item in libraries.values()}) != 3:
                raise ValueError("RAG 三个知识库的 knowledge_id 不得重复")
            if not settings.api_token.get_secret_value().strip():
                raise ValueError("RAG_API_TOKEN 不能为空")
            if not self._backends:
                self._backends = {
                    name: self._build_backend(item.top_n, item.knowledge_id.strip())
                    for name, item in libraries.items()
                }
            results = await asyncio.gather(*(
                self._backends[name].search(query, item.top_n, {"tags": tags or []})
                for name, item in libraries.items()
            ), return_exceptions=True)
            # A failed library must not be silently presented as an empty result.
            failures = [name for name, result in zip(libraries, results) if isinstance(result, BaseException)]
            if failures:
                raise RuntimeError("知识库检索失败：" + "、".join(failures))
            chunks = [chunk for result in results for chunk in result]
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
        backends = list(self._backends.values())
        if self._backend is not None:
            backends.append(self._backend)
        try:
            await asyncio.gather(*(backend.close() for backend in backends))
        finally:
            self._backends = {}
            self._backend = None
