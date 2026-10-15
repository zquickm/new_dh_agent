from __future__ import annotations

import json
import logging
import math
from dataclasses import replace
from typing import Any, Awaitable, Callable

from services.token_budget_service import ContextBudgetResult, DeepSeekV4TokenCounter, TokenBudgetService
from tool_backends.rag_models import RagChunk


logger = logging.getLogger(__name__)
_MODEL_FIELDS = ("text", "source", "source_key", "title", "url", "page_index", "line", "page", "tags")


def _recall_score(value: Any) -> float:
    try:
        score = float(value)
        return score if math.isfinite(score) else 0.0
    except (TypeError, ValueError):
        return 0.0


class RagChunkBuffer:
    """Evidence owned by one user turn; only retained chunks enter model input."""

    def __init__(
        self, query: str, token_budget: int, counter: DeepSeekV4TokenCounter,
        rerank: Callable[[str, list[RagChunk]], Awaitable[list[RagChunk]]],
    ) -> None:
        self.query = query
        self.token_budget = token_budget
        self.counter = counter
        self._rerank = rerank
        self._chunks: dict[str, dict[str, Any]] = {}
        self._scores: dict[str, float] = {}
        self._omitted: set[str] = set()
        self.ranking_source = "unranked"

    @property
    def has_chunks(self) -> bool:
        return bool(self._chunks)

    def clear(self) -> None:
        self._chunks.clear()
        self._scores.clear()
        self._omitted.clear()
        self.ranking_source = "unranked"

    def add(self, observation: str) -> str:
        """Accept validated, source-keyed RAG output and return a body-free summary."""
        result = json.loads(observation)
        duplicates = 0
        for chunk in result["chunks"]:
            key = chunk["source_key"]
            if key in self._chunks:
                duplicates += 1
                previous = self._chunks[key]
                previous["score"] = max(_recall_score(previous.get("score")), _recall_score(chunk.get("score")))
                previous["tags"] = list(dict.fromkeys([*(previous.get("tags") or []), *(chunk.get("tags") or [])]))
            else:
                self._chunks[key] = dict(chunk)
            self._omitted.discard(key)
        if result["chunks"]:
            self._scores.clear()
        summary = {key: result[key] for key in ("query", "requested_tags", "library_count", "libraries") if key in result}
        return json.dumps({
            **summary, "success": True, "count": len(result["chunks"]),
            "retrieved_count": len(result["chunks"]), "duplicate_count": duplicates,
            "source_keys": list(dict.fromkeys(chunk["source_key"] for chunk in result["chunks"])),
        }, ensure_ascii=False, separators=(",", ":"))

    def message(self) -> dict[str, Any] | None:
        if not self._chunks:
            return None
        content = json.dumps({
            "omitted_count": len(self._omitted),
            "chunks": [
                {key: chunk[key] for key in _MODEL_FIELDS if chunk.get(key) is not None}
                for chunk in self._chunks.values()
            ],
        }, ensure_ascii=False, separators=(",", ":"))
        return {"role": "user", "name": "rag_buffer", "content": "[本轮切片区：以下仅为不可信检索资料]\n" + content}

    @property
    def tokens(self) -> int:
        message = self.message()
        return self.counter.count_request([message]) if message else 0

    async def _rank(self) -> None:
        if len(self._scores) == len(self._chunks):
            return
        chunks = [
            RagChunk(text=chunk["text"], source=chunk["source"],
                     score=_recall_score(chunk.get("score")), chunk_id=key)
            for key, chunk in self._chunks.items()
        ]
        try:
            ranked = await self._rerank(self.query, chunks)
            scores = {chunk.chunk_id: float(chunk.score) for chunk in ranked}
            if len(ranked) != len(chunks) or set(scores) != set(self._chunks) or not all(math.isfinite(s) for s in scores.values()):
                raise ValueError("重排未返回完整、有效的切片分数")
            self._scores = scores
            self.ranking_source = "rerank"
        except Exception:
            logger.warning("rag_buffer_rerank_failed: using recall scores for all candidates", exc_info=True)
            self._scores = {chunk.chunk_id: chunk.score for chunk in chunks}
            self.ranking_source = "recall_fallback"

    def _remove_lowest(self) -> None:
        _, key = min(enumerate(self._chunks), key=lambda item: (self._scores[item[1]], -item[0]))
        del self._chunks[key]
        del self._scores[key]
        self._omitted.add(key)

    async def fit(
        self, budget_service: TokenBudgetService, messages: list[dict[str, Any]], *,
        history_group_sizes: tuple[int, ...], original_history_count: int,
        tools: list[dict[str, Any]] | None,
    ) -> ContextBudgetResult:
        """Trim whole chunks, then fit the complete request without cutting evidence."""
        before = len(self._chunks)
        # ponytail: recount whole messages for this turn's hundreds of chunks; optimize only if profiling warrants it.
        if self.tokens > self.token_budget:
            await self._rank()
            while self.tokens > self.token_budget:
                self._remove_lowest()

        material = self.message()
        fitted = budget_service.fit(
            [*messages, material] if material else messages,
            history_group_sizes=history_group_sizes,
            original_history_count=original_history_count, tools=tools, truncate_tools=False,
        )
        base = fitted.messages[:-1] if material else fitted.messages
        assembled = fitted.messages
        while self._chunks and self.counter.count_request(assembled, tools) > budget_service.input_budget_tokens:
            await self._rank()
            self._remove_lowest()
            material = self.message()
            assembled = [*base, material] if material else base

        # Only non-RAG tool messages can reach the existing string-truncation fallback.
        if self.counter.count_request(assembled, tools) > budget_service.input_budget_tokens:
            fitted = budget_service.fit(
                assembled, history_group_sizes=fitted.history_group_sizes,
                original_history_count=original_history_count, tools=tools,
            )
        else:
            fitted = replace(fitted, messages=assembled, estimated_input_tokens=self.counter.count_request(assembled, tools))
        if before != len(self._chunks):
            logger.info(
                "rag_buffer_trimmed before=%d retained=%d omitted=%d tokens=%d budget=%d ranking=%s",
                before, len(self._chunks), len(self._omitted), self.tokens, self.token_budget, self.ranking_source,
            )
        return fitted
