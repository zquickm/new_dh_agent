from __future__ import annotations

import logging
from typing import Any, Optional

from agents.tools.context import ToolContext
from config.settings import get_settings
from services.business_scenario import is_xiamen_query, resolve_query_scenario
from services.synonym_service import expand_query_synonyms
from tool_backends.rag_backend import RagSearchBackend
from tool_backends.rag_models import unique_file_sources


logger = logging.getLogger(__name__)


TOOL_NAME = "rag_search"
TOOL_DESCRIPTION = (
    "检索东海局和厦门局内部知识库中的文档、制度、报告或说明，返回相关片段及来源。"
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


def rag_display_name(query: str) -> str:
    query = expand_query_synonyms(query) if isinstance(query, str) else ""
    return "厦门局业务知识库" if is_xiamen_query(query) else "东溟知识库检索"


class RagSearchTool:
    """Adapt the RAG backend to the agent tool contract."""

    def __init__(self, backend: Optional[RagSearchBackend] = None) -> None:
        self._backend = backend

    @property
    def backend(self) -> RagSearchBackend:
        if self._backend is None:
            application = get_settings()
            settings = application.tools.rag
            scenarios = application.business_scenarios
            rerank = settings.rerank
            libraries = [
                library for scenario in scenarios.values()
                for library in scenario.rag.knowledge_bases
            ]
            self._backend = RagSearchBackend(
                recall_url=settings.resolved_recall_url(),
                api_token=settings.api_token.get_secret_value(),
                tenant_id=settings.tenant_id,
                knowledge_ids=tuple(dict.fromkeys(
                    library.knowledge_id.strip() for library in libraries if library.knowledge_id.strip()
                )),
                dept_ids=tuple(settings.dept_ids),
                score=scenarios["general"].rag.score,
                result_score_threshold=scenarios["general"].rag.result_score_threshold,
                candidate_top_n=max((library.recall_top_n for library in scenarios["general"].rag.knowledge_bases), default=20),
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
        *, context: ToolContext = ToolContext(),
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
        settings = get_settings().business_scenarios
        scenario_name = resolve_query_scenario(query, context.rag_scenario or "general")
        logger.info(
            "rag_scenario_selected base_rag_scenario=%s rag_scenario=%s query=%r",
            context.rag_scenario or "general", scenario_name, query,
        )
        business_scenario = settings.get(scenario_name)
        if business_scenario is None or not business_scenario.rag.knowledge_bases:
            raise ValueError("请配置当前场景的 RAG 知识库参数")
        scenario = business_scenario.rag
        chunks = await self.backend.search_all(
            query, [(library.knowledge_id.strip(), library.recall_top_n, library.top_n)
                    for library in scenario.knowledge_bases],
            {"tags": tags or []},
            score=scenario.score,
            result_score_threshold=scenario.result_score_threshold,
        )
        return {
            "query": query,
            "library_count": len(scenario.knowledge_bases),
            "libraries": [
                {
                    "knowledge_id": library.knowledge_id.strip(),
                    "name": library.name or library.knowledge_id.strip(),
                    "file_count": len(unique_file_sources(
                        chunk.source for chunk in chunks
                        if chunk.knowledge_id == library.knowledge_id.strip()
                    )),
                }
                for library in scenario.knowledge_bases
            ],
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
