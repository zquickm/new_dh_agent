from __future__ import annotations

import hashlib
import json
from typing import Any

from schemas.citation import RagBufferEntry


class RagBufferService:
    def __init__(self, repository) -> None:
        self._repository = repository

    async def list_entries(
        self, conversation_id: str, *, user_id: str
    ) -> list[RagBufferEntry]:
        return await self._repository.list_rag_buffer(
            conversation_id, user_id=user_id
        )

    async def persist_chunks(
        self,
        conversation_id: str,
        chunks: list[dict[str, Any]],
        *,
        user_id: str,
        query: str,
        run_id: str,
    ) -> list[RagBufferEntry]:
        normalized = [self._normalize_chunk(chunk) for chunk in chunks]
        return await self._repository.upsert_rag_buffer(
            conversation_id,
            normalized,
            user_id=user_id,
            query=query,
            run_id=run_id,
        )

    @staticmethod
    def _normalize_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        text = str(chunk.get("text") or "").strip()
        source = str(chunk.get("source") or "").strip()
        if not text or not source:
            raise ValueError("RAG 片段缺少正文或来源")
        identity = {
            "knowledge_id": RagBufferService._optional_string(
                chunk.get("knowledge_id")
            ),
            "document_id": RagBufferService._optional_string(
                chunk.get("document_id")
            ),
            "chunk_id": RagBufferService._optional_string(chunk.get("chunk_id")),
            "source": source.casefold(),
            "page_index": RagBufferService._optional_int(chunk.get("page_index")),
            "line": RagBufferService._optional_int(chunk.get("line")),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        return {
            **chunk,
            "text": text,
            "source": source,
            "knowledge_id": identity["knowledge_id"],
            "document_id": identity["document_id"],
            "chunk_id": identity["chunk_id"],
            "page_index": identity["page_index"],
            "line": identity["line"],
            "page": RagBufferService._optional_int(chunk.get("page")),
            "identity_hash": hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None
