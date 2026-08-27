from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from schemas.chat import FrontendContext
from schemas.conversation import (
    ConversationDetail,
    ConversationMessage,
    ConversationSummary,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryConversationRepository:
    def __init__(self) -> None:
        self._items: dict[str, ConversationDetail] = {}
        self._map_contexts: dict[str, FrontendContext] = {}
        self._lock = asyncio.Lock()

    async def create(self, title: str) -> ConversationDetail:
        now = _now()
        item = ConversationDetail(
            id=uuid4().hex,
            title=title.strip() or "新会话",
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._items[item.id] = item
        return item.model_copy(deep=True)

    async def get(self, conversation_id: str) -> Optional[ConversationDetail]:
        async with self._lock:
            item = self._items.get(conversation_id)
            return item.model_copy(deep=True) if item else None

    async def list(self, limit: int) -> list[ConversationSummary]:
        async with self._lock:
            items = sorted(
                self._items.values(), key=lambda item: item.updated_at, reverse=True
            )[:limit]
            return [ConversationSummary(**item.model_dump()) for item in items]

    async def get_map_context(
        self,
        conversation_id: str,
    ) -> Optional[FrontendContext]:
        async with self._lock:
            context = self._map_contexts.get(conversation_id)
            return context.model_copy(deep=True) if context is not None else None

    async def save_map_context(
        self,
        conversation_id: str,
        context: FrontendContext,
    ) -> None:
        async with self._lock:
            if conversation_id not in self._items:
                raise KeyError(conversation_id)
            self._map_contexts[conversation_id] = context.model_copy(deep=True)

    async def save_exchange(
        self,
        conversation_id: str,
        question: str,
        answer: str,
        sources: list[dict],
    ) -> ConversationMessage:
        now = _now()
        user = ConversationMessage(
            id=uuid4().hex,
            role="user",
            content=question,
            created_at=now,
        )
        assistant = ConversationMessage(
            id=uuid4().hex,
            role="assistant",
            content=answer,
            created_at=now,
            sources=sources,
        )
        async with self._lock:
            item = self._items.get(conversation_id)
            if item is None:
                raise KeyError(conversation_id)
            item.messages.extend((user, assistant))
            item.message_count = len(item.messages)
            item.updated_at = now
            if item.title == "新会话":
                item.title = question[:30]
        return assistant


conversation_repository = MemoryConversationRepository()
