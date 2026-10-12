from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


def unique_file_sources(sources: Iterable[str]) -> list[str]:
    """按来源文件名去重，保留首次出现顺序；展示截断由调用方处理。"""
    return list(dict.fromkeys(sources))


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
