from __future__ import annotations

import json
import re
from typing import Any, Optional


_CITATION_PATTERN = re.compile(r"\[\[cite:(\d+)\]\]", re.IGNORECASE)
_SOURCE_TOKEN_PATTERN = re.compile(r"<<SRC_(\d+)>>", re.IGNORECASE)
_SOURCE_TOKEN_OPENER = "<<src_"
_RAW_CITATION_OPENER = "[[cite:"


def strip_citation_markers(text: str) -> str:
    """Remove turn-local citation markers before reusing conversation history."""
    return _SOURCE_TOKEN_PATTERN.sub("", _CITATION_PATTERN.sub("", text))


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def safe_model_text(text: str) -> str:
    # Retrieved documents are untrusted data. Prevent their contents from
    # introducing source markers that were not assigned by this registry.
    text = re.sub(r"<<\s*SRC_", "<SRC_", text, flags=re.IGNORECASE)
    return re.sub(r"\[\[\s*cite:", "[cite:", text, flags=re.IGNORECASE)


class CitationRegistry:
    """Conversation source catalog plus request-local display numbering."""

    def __init__(self, sources: Optional[list[dict[str, Any]]] = None) -> None:
        self._sources: list[dict[str, Any]] = []
        self._known: dict[tuple[str, Optional[int], str], dict[str, Any]] = {}
        self._display_ids: dict[int, int] = {}
        for source in sources or []:
            self._register_persisted_source(source)

    @property
    def has_sources(self) -> bool:
        return bool(self._sources)

    def sources(self, *, display_ids: bool = False) -> list[dict[str, Any]]:
        result = []
        for index, source in enumerate(self._sources, start=1):
            item = dict(source)
            buffer_id = item.get("buffer_id")
            item["citation_id"] = (
                self._display_ids.get(int(buffer_id)) if display_ids and buffer_id is not None
                else None if display_ids
                else index
            )
            result.append(item)
        return result

    def register_database_observation(self, observation: str, *, question: str) -> None:
        try:
            result = json.loads(observation)
        except (TypeError, json.JSONDecodeError):
            return
        if not isinstance(result, dict) or result.get("success") is not True:
            return
        rows = result.get("rows")
        if not isinstance(rows, list) or not any(
            isinstance(row, dict) and any(
                value is not None and value != "" for value in row.values()
            ) for row in rows
        ):
            return
        source = next(
            (item for item in self._sources if item.get("source_type") == "database"),
            None,
        )
        if source is None:
            source = {
                "buffer_id": None,
                "source": "断面数据库",
                "source_type": "database",
                "title": "断面观测数据",
                "preview": "",
                "url": None,
                "line": None,
                "page_index": None,
                "score": 0.0,
            }
            self._sources.append(source)
        preview = f"查询：{question}；返回 {len(rows)} 行结果"
        if result.get("truncated"):
            preview += "（结果截断）"
        source["preview"] = "\n\n".join(filter(None, (source["preview"], preview)))

    def register_rag_observation(
        self,
        observation: str,
        *,
        tool_call_id: str,
        fallback_query: Any,
        persisted_sources: Optional[list[Any]] = None,
    ) -> tuple[dict[str, Any], str]:
        try:
            result = json.loads(observation)
        except json.JSONDecodeError as exc:
            raise ValueError("rag_search returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(result.get("chunks"), list):
            raise ValueError("rag_search returned an invalid result")

        retrieval_chunks: list[dict[str, Any]] = []
        model_chunks: list[dict[str, Any]] = []
        if persisted_sources is not None and len(persisted_sources) != len(result["chunks"]):
            raise ValueError("持久化 RAG 片段数量与工具结果不一致")
        for rank, item in enumerate(result["chunks"], start=1):
            if not isinstance(item, dict):
                raise ValueError("rag_search returned an invalid chunk")
            text = item.get("text")
            source_name = item.get("source")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("rag_search returned a chunk without text")
            if not isinstance(source_name, str) or not source_name.strip():
                raise ValueError("rag_search returned a chunk without source")

            persisted = (
                persisted_sources[rank - 1] if persisted_sources is not None else None
            )
            if persisted is not None:
                if hasattr(persisted, "model_dump"):
                    persisted = persisted.model_dump()
                source = self._register_persisted_source({
                    **dict(persisted),
                    "preview": dict(persisted).get("text", text),
                })
            else:
                source = self._register_source(item, text, source_name)
            score = _score(item.get("score"))
            retrieval_chunks.append(
                {
                    "rank": rank,
                    "buffer_id": source["buffer_id"],
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
                    "text": safe_model_text(text),
                    "score": score,
                    "source_key": f"<<SRC_{source['buffer_id']}>>",
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
        content, pending = self.transform_stream_buffer(answer, final=True)
        if pending:
            raise ValueError("最终模型输出了不完整的来源键")
        return content

    def transform_stream_buffer(
        self,
        buffer: str,
        *,
        final: bool,
    ) -> tuple[str, str]:
        registry = self._source_registry()
        output: list[str] = []
        position = 0
        folded = buffer.casefold()
        while position < len(buffer):
            source_index = folded.find(_SOURCE_TOKEN_OPENER, position)
            citation_index = folded.find(_RAW_CITATION_OPENER, position)
            marker_indexes = [
                index for index in (source_index, citation_index) if index >= 0
            ]
            if not marker_indexes:
                remainder = buffer[position:]
                if final:
                    output.append(remainder)
                    return "".join(output), ""
                keep = self._partial_opener_length(remainder)
                if keep:
                    output.append(remainder[:-keep])
                    return "".join(output), remainder[-keep:]
                output.append(remainder)
                return "".join(output), ""

            marker_index = min(marker_indexes)
            output.append(buffer[position:marker_index])
            if marker_index == citation_index:
                close_index = folded.find("]]", marker_index + 2)
                if close_index < 0 and not final:
                    return "".join(output), buffer[marker_index:]
                raise ValueError("最终模型不得直接输出数字引用标记")

            close_index = folded.find(">>", marker_index + len(_SOURCE_TOKEN_OPENER))
            if close_index < 0:
                if not final:
                    return "".join(output), buffer[marker_index:]
                raise ValueError("最终模型输出了不完整的来源键")
            token = buffer[marker_index : close_index + 2]
            match = _SOURCE_TOKEN_PATTERN.fullmatch(token)
            if match is None:
                raise ValueError(f"最终模型输出了非法来源键：{token}")
            key = f"SRC_{match.group(1)}"
            source = registry.get(key)
            if source is None:
                raise ValueError(f"最终模型输出了未知来源键：{token}")
            buffer_id = int(source["buffer_id"])
            display_id = self._display_ids.setdefault(
                buffer_id, len(self._display_ids) + 1
            )
            output.append(f"[[cite:{display_id}]]")
            position = close_index + 2

        return "".join(output), ""

    @staticmethod
    def _partial_opener_length(text: str) -> int:
        folded = text.casefold()
        lengths = (
            length
            for opener in (_SOURCE_TOKEN_OPENER, _RAW_CITATION_OPENER)
            for length in range(1, len(opener))
            if folded.endswith(opener[:length])
        )
        return max(lengths, default=0)

    def _source_registry(self) -> dict[str, dict[str, Any]]:
        registry: dict[str, dict[str, Any]] = {}
        for source in self._sources:
            if source.get("buffer_id") is None:
                continue
            buffer_id = int(source["buffer_id"])
            key = f"SRC_{buffer_id}"
            if buffer_id <= 0 or key in registry:
                raise ValueError(f"引用缓冲区编号无效或重复：{buffer_id}")
            registry[key] = source
        return registry

    def _register_persisted_source(self, value: dict[str, Any]) -> dict[str, Any]:
        buffer_id = _optional_int(value.get("buffer_id"))
        if buffer_id is None or buffer_id <= 0:
            raise ValueError("持久化引用来源缺少有效 buffer_id")
        existing = next(
            (item for item in self._sources if item.get("buffer_id") == buffer_id),
            None,
        )
        if existing is not None:
            return existing
        source_name = str(value.get("source") or "").strip()
        preview = str(value.get("preview") or value.get("text") or "").strip()
        if not source_name or not preview:
            raise ValueError("持久化引用来源缺少来源名称或正文")
        source = {
            "buffer_id": buffer_id,
            "source": source_name,
            "line": _optional_int(value.get("line")),
            "score": _score(value.get("score")),
            "preview": preview,
            "title": value.get("title"),
            "url": value.get("url"),
            "page_index": _optional_int(value.get("page_index")),
            "page": _optional_int(value.get("page")),
            "document_id": value.get("document_id"),
            "chunk_id": value.get("chunk_id"),
            "knowledge_id": value.get("knowledge_id"),
        }
        self._sources.append(source)
        return source

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
            or source_name
        ).strip().casefold()
        identity = (document, location, source_name.strip().casefold())
        source = self._known.get(identity)
        if source is None:
            existing_ids = [
                int(item["buffer_id"])
                for item in self._sources if item.get("buffer_id") is not None
            ]
            source = {
                "buffer_id": max(existing_ids, default=0) + 1,
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
