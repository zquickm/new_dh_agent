from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Optional, Protocol

import aiohttp

from tool_backends.rag_models import RagChunk, RagToolError


logger = logging.getLogger(__name__)

PREFIX = (
    "system\n"
    "Judge whether the Document meets the requirements based on the Query "
    "and the Instruct provided. Note that the answer can only be \"yes\" or \"no\"."
    "\n"
    "user\n"
)
SUFFIX = "\n"
INSTRUCTION = "根据用户问题，判断文档是否包含能够回答问题的相关内容"


class DocumentReranker(Protocol):
    async def rerank(
        self,
        query: str,
        chunks: list[RagChunk],
        top_n: int,
    ) -> list[RagChunk]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class VllmRerankerSettings:
    url: str
    model: str
    api_token: str
    timeout: float = 15
    connect_timeout: float = 5
    max_retries: int = 1
    retry_backoff: float = 0.3
    verify_ssl: bool = True


class RerankResponseParser:
    def parse(
        self,
        body: Any,
        chunks: list[RagChunk],
        top_n: int,
    ) -> list[RagChunk]:
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise RagToolError("Rerank service returned an invalid response")

        ranked: list[tuple[float, RagChunk]] = []
        seen: set[int] = set()
        for item in body["results"]:
            if not isinstance(item, dict):
                raise RagToolError("Rerank result must be an object")
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise RagToolError("Rerank result contains an invalid index")
            if index < 0 or index >= len(chunks) or index in seen:
                raise RagToolError("Rerank result contains an invalid index")
            try:
                score = float(item["relevance_score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RagToolError("Rerank result contains an invalid score") from exc
            seen.add(index)
            ranked.append((score, replace(chunks[index], score=score)))

        expected_count = min(top_n, len(chunks))
        if len(ranked) != expected_count:
            raise RagToolError(
                f"Rerank service returned {len(ranked)} results; "
                f"expected {expected_count}"
            )
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [chunk for _, chunk in ranked]


class VllmDocumentReranker:
    """Call the vLLM rerank endpoint and map scores onto recalled chunks."""

    def __init__(
        self,
        settings: VllmRerankerSettings,
        session: Optional[Any] = None,
    ) -> None:
        if not settings.url or not settings.model or not settings.api_token:
            raise RagToolError(
                "RERANK_URL/RERANK_MODEL/RAG_API_TOKEN are required"
            )
        if (
            settings.timeout <= 0
            or settings.connect_timeout <= 0
            or settings.max_retries < 0
            or settings.retry_backoff < 0
        ):
            raise RagToolError("Rerank numeric settings are invalid")
        self.settings = settings
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()
        self._parser = RerankResponseParser()

    async def rerank(
        self,
        query: str,
        chunks: list[RagChunk],
        top_n: int,
    ) -> list[RagChunk]:
        if not chunks:
            return []
        result_limit = min(max(1, top_n), len(chunks))
        payload = {
            "model": self.settings.model,
            "query": f"{PREFIX}<Instruct>: {INSTRUCTION}\n<Query>: {query}\n",
            "documents": [f"<Document>: {chunk.text}{SUFFIX}" for chunk in chunks],
            "top_n": result_limit,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        logger.info(
            "Rerank request started: model=%s candidate_count=%s top_n=%s",
            self.settings.model,
            len(chunks),
            result_limit,
        )
        try:
            body = await self._post_json(payload, headers)
            result = self._parser.parse(body, chunks, result_limit)
        except Exception:
            logger.exception(
                "Rerank request failed: model=%s candidate_count=%s top_n=%s "
                "duration_ms=%.1f",
                self.settings.model,
                len(chunks),
                result_limit,
                (time.monotonic() - started) * 1000,
            )
            raise
        logger.info(
            "Rerank request completed: model=%s candidate_count=%s result_count=%s "
            "duration_ms=%.1f",
            self.settings.model,
            len(chunks),
            len(result),
            (time.monotonic() - started) * 1000,
        )
        return result

    async def _post_json(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> Any:
        last_error: Optional[RagToolError] = None
        last_cause: Optional[BaseException] = None
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            retryable = True
            try:
                session = await self._get_session()
                async with session.post(
                    self.settings.url,
                    json=payload,
                    headers=headers,
                    ssl=self.settings.verify_ssl,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        detail = (await response.text())[:300]
                        last_error = RagToolError(
                            f"Rerank service returned HTTP {response.status}: {detail}"
                        )
                        retryable = response.status == 429 or response.status >= 500
                    else:
                        try:
                            return await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, ValueError) as exc:
                            last_error = RagToolError(
                                "Rerank service returned invalid JSON"
                            )
                            last_cause = exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = RagToolError(f"Rerank service request failed: {exc}")
                last_cause = exc
            if not retryable or attempt + 1 >= attempts:
                break
            delay = self.settings.retry_backoff * (2**attempt)
            logger.warning(
                "Rerank attempt %s/%s failed; retrying in %.2fs: error=%s",
                attempt + 1,
                attempts,
                delay,
                last_error,
            )
            if delay:
                await asyncio.sleep(delay)
        raise (last_error or RagToolError("Rerank service request failed")) from last_cause

    async def close(self) -> None:
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> Any:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(
                    total=self.settings.timeout,
                    connect=self.settings.connect_timeout,
                )
                self._session = aiohttp.ClientSession(
                    timeout=timeout,
                    connector=aiohttp.TCPConnector(limit=10, ttl_dns_cache=300),
                    raise_for_status=False,
                )
        return self._session
