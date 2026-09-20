from typing import Optional

from pydantic import BaseModel


class RagBufferEntry(BaseModel):
    buffer_id: int
    identity_hash: str
    text: str
    source: str
    score: float = 0.0
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    knowledge_id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    page_index: Optional[int] = None
    line: Optional[int] = None
    page: Optional[int] = None

    def source_dict(self) -> dict:
        return {
            "buffer_id": self.buffer_id,
            "source": self.source,
            "score": self.score,
            "preview": self.text,
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "knowledge_id": self.knowledge_id,
            "title": self.title,
            "url": self.url,
            "page_index": self.page_index,
            "line": self.line,
            "page": self.page,
        }
