from __future__ import annotations

import json
import math
import re
from typing import Any, Optional


_CITATION_PATTERN = re.compile(r"\[\[cite:(\d+)\]\]", re.IGNORECASE)
_SOURCE_TOKEN_PATTERN = re.compile(r"<<SRC_(\d+)>>", re.IGNORECASE)
_MALFORMED_SOURCE_TOKEN_PATTERN = re.compile(r"<<\s*SRC_[^>]*>>", re.IGNORECASE)
_INCOMPLETE_SOURCE_TOKEN_PATTERN = re.compile(
    r"<<\s*SRC_(?!\d+>>)[^\r\n]*", re.IGNORECASE
)
_INCOMPLETE_CITATION_PATTERN = re.compile(
    r"\[\[\s*cite:(?!\d+\]\])[^\r\n]*", re.IGNORECASE
)


def strip_citation_markers(text: str) -> str:
    """Remove turn-local citation markers before reusing conversation history."""
    return _CITATION_PATTERN.sub("", text)


def _score(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_model_text(text: str) -> str:
    # Retrieved documents are untrusted data. Prevent their contents from
    # introducing source markers that were not assigned by this registry.
    text = re.sub(r"<<\s*SRC_", "<SRC_", text, flags=re.IGNORECASE)
    return re.sub(r"\[\[\s*cite:", "[cite:", text, flags=re.IGNORECASE)


class CitationRegistry:
    """Request-local mapping between retrieved RAG chunks and citation IDs."""

    def __init__(self) -> None:
        self._sources: list[dict[str, Any]] = []
        self._known: dict[tuple[str, Optional[int], str], dict[str, Any]] = {}

    @property
    def has_sources(self) -> bool:
        return bool(self._sources)

    def sources(self) -> list[dict[str, Any]]:
        return [dict(source) for source in self._sources]

    def register_rag_observation(
        self,
        observation: str,
        *,
        tool_call_id: str,
        fallback_query: Any,
    ) -> tuple[dict[str, Any], str]:
        try:
            result = json.loads(observation)
        except json.JSONDecodeError as exc:
            raise ValueError("rag_search returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("chunks"), list):
            raise ValueError("rag_search returned an invalid result")

        retrieval_chunks: list[dict[str, Any]] = []
        model_chunks: list[dict[str, Any]] = []
        for rank, item in enumerate(result["chunks"], start=1):
            if not isinstance(item, dict):
                raise ValueError("rag_search returned an invalid chunk")
            text = item.get("text")
            source_name = item.get("source")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("rag_search returned a chunk without text")
            if not isinstance(source_name, str) or not source_name.strip():
                raise ValueError("rag_search returned a chunk without source")

            source = self._register_source(item, text, source_name)
            score = _score(item.get("score"))
            retrieval_chunks.append(
                {
                    "rank": rank,
                    "citation_id": source["citation_id"],
                    "source": source_name,
                    "preview": text,
                    "score": score,
                    "document_id": item.get("document_id"),
                    "chunk_id": item.get("chunk_id"),
                    "knowledge_id": item.get("knowledge_id"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "page_index": item.get("page_index"),
                    "line": item.get("line"),
                    "page": item.get("page"),
                }
            )
            model_chunks.append(
                {
                    **item,
                    "text": _safe_model_text(text),
                    "score": score,
                    "source_key": f"<<SRC_{source['citation_id']}>>",
                }
            )

        query = result.get("query")
        if not isinstance(query, str):
            query = str(fallback_query or "")
        retrieval = {
            "tool_call_id": tool_call_id,
            "tool_name": "rag_search",
            "query": query,
            "result_count": len(retrieval_chunks),
            "chunks": retrieval_chunks,
        }
        model_result = {**result, "chunks": model_chunks}
        return retrieval, json.dumps(model_result, ensure_ascii=False, default=str)

    def transform_answer(self, answer: str) -> str:
        valid_ids = {
            int(source["citation_id"])
            for source in self._sources
            if source.get("citation_id") is not None
        }

        def replace_source_key(match: re.Match[str]) -> str:
            citation_id = int(match.group(1))
            return f"[[cite:{citation_id}]]" if citation_id in valid_ids else ""

        def remove_invalid_citation(match: re.Match[str]) -> str:
            return match.group(0) if int(match.group(1)) in valid_ids else ""

        answer = _SOURCE_TOKEN_PATTERN.sub(replace_source_key, answer)
        answer = _MALFORMED_SOURCE_TOKEN_PATTERN.sub("", answer)
        answer = _INCOMPLETE_SOURCE_TOKEN_PATTERN.sub("", answer)
        answer = _CITATION_PATTERN.sub(remove_invalid_citation, answer)
        return _INCOMPLETE_CITATION_PATTERN.sub("", answer)

    def _register_source(
        self,
        item: dict[str, Any],
        text: str,
        source_name: str,
    ) -> dict[str, Any]:
        page_index = _optional_int(item.get("page_index"))
        line = _optional_int(item.get("line"))
        location = page_index if page_index is not None else line
        document = str(
            item.get("knowledge_id")
            or item.get("document_id")
            or source_name
        ).strip().casefold()
        identity = (document, location, source_name.strip().casefold())
        source = self._known.get(identity)
        if source is None:
            source = {
                "citation_id": len(self._sources) + 1,
                "source": source_name,
                "line": line,
                "score": _score(item.get("score")),
                "preview": text,
                "title": item.get("title"),
                "url": item.get("url"),
                "page_index": page_index,
                "chunk_id": item.get("chunk_id"),
                "knowledge_id": item.get("knowledge_id"),
            }
            self._sources.append(source)
            self._known[identity] = source
            return source

        source["score"] = max(_score(source.get("score")), _score(item.get("score")))
        if text not in str(source.get("preview") or ""):
            source["preview"] = f"{source['preview']}\n\n{text}"
        return source
