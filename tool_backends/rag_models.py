from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class RagToolError(RuntimeError):
    """A safe error raised by the RAG tool backend."""


@dataclass(frozen=True)
class RagChunk:
    text: str
    source: str
    score: float
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    knowledge_id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    page_index: Optional[int] = None
    line: Optional[int] = None
    page: Optional[int] = None
