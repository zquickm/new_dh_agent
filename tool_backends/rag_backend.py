from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import aiohttp

from tool_backends.rag_models import RagChunk, RagToolError
from tool_backends.rerank_backend import (
    DocumentReranker,
    VllmDocumentReranker,
    VllmRerankerSettings,
)


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RagToolError(f"{name} must be true or false")


def _env_list(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default).strip()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _first_text(*values: Any) -> Optional[str]:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class RagSearchBackend:
    """Asynchronous knowledge-base recall with optional vLLM reranking."""

    def __init__(
        self,
        *,
        recall_url: str,
        api_token: str,
        tenant_id: str,
        knowledge_ids: tuple[str, ...],
        dept_ids: tuple[str, ...],
        score: float,
        candidate_top_n: int,
        faq_score: float,
        search_method: int,
        search_scope: str,
        timeout: float,
        connect_timeout: float,
        connection_limit: int,
        verify_ssl: bool,
        max_retries: int,
        retry_backoff: float,
        rerank_enabled: bool,
        rerank_url: str,
        rerank_model: str,
        rerank_timeout: float,
        rerank_connect_timeout: float,
        rerank_max_retries: int,
        rerank_retry_backoff: float,
        rerank_verify_ssl: bool,
        reranker: Optional[DocumentReranker] = None,
    ) -> None:
        if not recall_url or not api_token or not tenant_id or not knowledge_ids:
            raise RagToolError(
                "RAG_RECALL_URL/RAG_API_TOKEN/RAG_TENANT_ID/RAG_KNOWLEDGE_IDS "
                "are required"
            )
        self.recall_url = recall_url
        self.api_token = api_token
        self.tenant_id = tenant_id
        self.knowledge_ids = knowledge_ids
        self.dept_ids = dept_ids
        self.score = score
        self.candidate_top_n = max(1, candidate_top_n)
        self.faq_score = faq_score
        self.search_method = search_method
        self.search_scope = search_scope
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.connection_limit = connection_limit
        self.verify_ssl = verify_ssl
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.rerank_enabled = rerank_enabled
        self.reranker = reranker if rerank_enabled else None
        if rerank_enabled and self.reranker is None:
            self.reranker = VllmDocumentReranker(
                VllmRerankerSettings(
                    url=rerank_url,
                    model=rerank_model,
                    api_token=api_token,
                    timeout=rerank_timeout,
                    connect_timeout=rerank_connect_timeout,
                    max_retries=rerank_max_retries,
                    retry_backoff=rerank_retry_backoff,
                    verify_ssl=rerank_verify_ssl,
                )
            )
        self._session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    @classmethod
    def from_env(cls) -> "RagSearchBackend":
        base_url = os.getenv("RAG_BASE_URL", "").strip()
        recall_path = os.getenv(
            "RAG_RECALL_PATH",
            "/rag-platform-default/zsgl/knowledgeRecall",
        ).strip()
        explicit_url = os.getenv("RAG_RECALL_URL", "").strip()
        recall_url = explicit_url or (
            f"{base_url.rstrip('/')}/{recall_path.lstrip('/')}" if base_url else ""
        )

        rerank_base_url = os.getenv("RERANK_BASE_URL", base_url).strip()
        rerank_path = os.getenv("RERANK_PATH", "/v1/rerank").strip()
        rerank_url = os.getenv("RERANK_URL", "").strip() or (
            f"{rerank_base_url.rstrip('/')}/{rerank_path.lstrip('/')}"
            if rerank_base_url
            else ""
        )
        rerank_enabled = _env_bool("RERANK_ENABLED", True)
        if rerank_enabled and not rerank_url:
            raise RagToolError("RERANK_URL or RERANK_BASE_URL is required")

        tenant_id = os.getenv("RAG_TENANT_ID", "").strip()
        return cls(
            recall_url=recall_url,
            api_token=os.getenv("RAG_API_TOKEN", "").strip(),
            tenant_id=tenant_id,
            knowledge_ids=_env_list("RAG_KNOWLEDGE_IDS"),
            dept_ids=_env_list("RAG_DEPT_IDS", tenant_id),
            score=float(os.getenv("RAG_SCORE", "0.6")),
            candidate_top_n=int(
                os.getenv(
                    "RERANK_CANDIDATE_TOP_N" if rerank_enabled else "RAG_TOP_N",
                    "100" if rerank_enabled else "20",
                )
            ),
            faq_score=float(os.getenv("RAG_FAQ_SCORE", "0.8")),
            search_method=int(os.getenv("RAG_SEARCH_METHOD", "3")),
            search_scope=os.getenv("RAG_SEARCH_SCOPE", "knowledge").strip(),
            timeout=float(os.getenv("RAG_TIMEOUT", "30")),
            connect_timeout=float(os.getenv("RAG_CONNECT_TIMEOUT", "5")),
            connection_limit=int(os.getenv("RAG_CONNECTION_LIMIT", "100")),
            verify_ssl=_env_bool("RAG_VERIFY_SSL", True),
            max_retries=int(os.getenv("RAG_MAX_RETRIES", "2")),
            retry_backoff=float(os.getenv("RAG_RETRY_BACKOFF", "0.5")),
            rerank_enabled=rerank_enabled,
            rerank_url=rerank_url,
            rerank_model=os.getenv("RERANK_MODEL", "Qwen3-Reranker-4B").strip(),
            rerank_timeout=float(os.getenv("RERANK_TIMEOUT", "15")),
            rerank_connect_timeout=float(
                os.getenv("RERANK_CONNECT_TIMEOUT", "5")
            ),
            rerank_max_retries=int(os.getenv("RERANK_MAX_RETRIES", "1")),
            rerank_retry_backoff=float(
                os.getenv("RERANK_RETRY_BACKOFF", "0.3")
            ),
            rerank_verify_ssl=_env_bool("RERANK_VERIFY_SSL", True),
        )

    async def search(
        self,
        query: str,
        top_k: int,
        filters: Optional[dict[str, Any]] = None,
    ) -> list[RagChunk]:
        query = query.strip()
        if not query:
            return []

        filters = filters or {}
        unknown = set(filters) - {"knowledge_ids", "dept_ids", "tags"}
        if unknown:
            raise RagToolError(
                "Unsupported RAG filters: " + ", ".join(sorted(unknown))
            )

        knowledge_ids = self._authorized_values(
            "knowledge_ids",
            filters.get("knowledge_ids"),
            self.knowledge_ids,
        )
        dept_ids = self._authorized_values(
            "dept_ids",
            filters.get("dept_ids"),
            self.dept_ids,
        )
        tags = self._string_list("tags", filters.get("tags", []))

        payload = {
            "tenant_id": self.tenant_id,
            "query": [query],
            "score": self.score,
            "top_n": self.candidate_top_n,
            "faq_score": self.faq_score,
            "rerank_enable": 0,
            "search_method": self.search_method,
            "tags": tags,
            "knowledge_ids": knowledge_ids,
            "faq_ids": [],
            "searchScope": self.search_scope,
            "dept_ids": dept_ids,
            "forceTagList": [],
        }
        body = await self._post_json(
            self.recall_url,
            payload,
            self._headers(),
            timeout=self.timeout,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
        )
        chunks = self._parse_recall(body, self.candidate_top_n)

        if not chunks:
            return []

        if self.reranker is not None:
            try:
                return await self.reranker.rerank(query, chunks, top_k)
            except RagToolError:
                # Recall order is a usable fallback when reranking is unavailable.
                logger.warning(
                    "Rerank failed; falling back to recall order: "
                    "candidate_count=%s result_count=%s",
                    len(chunks),
                    min(top_k, len(chunks)),
                    exc_info=True,
                )
                return chunks[:top_k]
        return chunks[:top_k]

    async def search_all(
        self, query: str, top_k: int, filters: Optional[dict[str, Any]] = None,
    ) -> list[RagChunk]:
        if len(self.knowledge_ids) != 3 or any(not value for value in self.knowledge_ids):
            raise RagToolError("请配置三个知识库的 knowledge_ids")
        if len(set(self.knowledge_ids)) != 3:
            raise RagToolError("三个知识库的 knowledge_ids 不得重复")
        results = await asyncio.gather(*(
            self.search(query, top_k, {**(filters or {}), "knowledge_ids": [knowledge_id]})
            for knowledge_id in self.knowledge_ids
        ), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise RagToolError("知识库检索失败") from result
        return [chunk for chunks in results for chunk in chunks]

    async def close(self) -> None:
        try:
            if self._session is not None and not self._session.closed:
                await self._session.close()
        finally:
            if self.reranker is not None:
                await self.reranker.close()

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        timeout: float,
        max_retries: int,
        retry_backoff: float,
    ) -> Any:
        last_error: Optional[BaseException] = None
        for attempt in range(max(0, max_retries) + 1):
            retryable = True
            try:
                session = await self._get_session()
                request_timeout = aiohttp.ClientTimeout(
                    total=timeout,
                    connect=min(timeout, self.connect_timeout),
                )
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    ssl=self.verify_ssl,
                    timeout=request_timeout,
                ) as response:
                    if 200 <= response.status < 300:
                        try:
                            return await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            raise RagToolError(
                                "RAG service returned invalid JSON"
                            ) from exc
                    detail = (await response.text())[:300]
                    last_error = RagToolError(
                        f"RAG service returned HTTP {response.status}: {detail}"
                    )
                    retryable = response.status == 429 or response.status >= 500
            except (aiohttp.ClientError, asyncio.TimeoutError, RagToolError) as exc:
                last_error = exc
            if not retryable or attempt >= max_retries:
                break
            delay = max(0.0, retry_backoff) * (2**attempt)
            if delay:
                await asyncio.sleep(delay)
        raise RagToolError("RAG service request failed") from last_error

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=max(1, self.connection_limit),
                    ttl_dns_cache=300,
                    enable_cleanup_closed=True,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    raise_for_status=False,
                )
        return self._session

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _parse_recall(body: Any, limit: int) -> list[RagChunk]:
        if not isinstance(body, dict):
            raise RagToolError("RAG service returned an invalid response")
        if str(body.get("code")) != "200":
            raise RagToolError(
                f"RAG service returned business error: {body.get('msg') or 'unknown'}"
            )
        data = body.get("data")
        records = data.get("records") if isinstance(data, dict) else None
        if not isinstance(records, list):
            raise RagToolError("RAG response is missing data.records")

        chunks: list[RagChunk] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            text = _first_text(
                record.get("chunk_text"),
                record.get("abstract_text"),
                record.get("question_text"),
            )
            if not text:
                continue
            page_index = _as_int(record.get("page_index"))
            line = _as_int(record.get("line"))
            if line is None:
                line = _as_int(record.get("line_no"))
            if line is None and page_index is not None:
                line = page_index + 1
            # Keep the legacy one-based display field, but preserve the
            # upstream zero-based page index and line as separate values.
            page = page_index + 1 if page_index is not None else line
            document_id = _first_text(
                record.get("doc_id"),
            )
            source = _first_text(
                record.get("fileName"),
                record.get("doc_title"),
                record.get("doc_name"),
                document_id,
            ) or "知识库文档"
            chunks.append(
                RagChunk(
                    text=text,
                    source=source,
                    score=_as_float(record.get("score")),
                    document_id=document_id,
                    chunk_id=_first_text(record.get("chunk_id")),
                    knowledge_id=_first_text(
                        record.get("knowledge_id"),
                        record.get("knowledgeId"),
                    ),
                    title=_first_text(
                        record.get("chunk_title"),
                        record.get("doc_title"),
                    ),
                    url=_first_text(record.get("downloadUrl")),
                    page_index=page_index,
                    line=line,
                    page=page,
                )
            )
            if len(chunks) >= limit:
                break
        return chunks

    @staticmethod
    def _string_list(name: str, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise RagToolError(f"{name} must be a string array")
        result = [str(item).strip() for item in value if str(item).strip()]
        if len(result) > 50:
            raise RagToolError(f"{name} contains too many values")
        return result

    def _authorized_values(
        self,
        name: str,
        requested: Any,
        configured: tuple[str, ...],
    ) -> list[str]:
        if requested is None:
            return list(configured)
        values = self._string_list(name, requested)
        if not values:
            return list(configured)
        unauthorized = set(values) - set(configured)
        if unauthorized:
            raise RagToolError(f"{name} contains unauthorized values")
        return values
