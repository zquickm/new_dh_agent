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
    """In-memory fake used only by unit tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ConversationDetail] = {}
        self._map_contexts: dict[tuple[str, str], FrontendContext] = {}
        self._lock = asyncio.Lock()

    async def create(self, title: str, *, user_id: str) -> ConversationDetail:
        now = _now()
        item = ConversationDetail(
            id=uuid4().hex,
            title=title.strip() or "新会话",
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._items[(user_id, item.id)] = item
        return item.model_copy(deep=True)

    async def get(
        self, conversation_id: str, *, user_id: str
    ) -> Optional[ConversationDetail]:
        async with self._lock:
            item = self._items.get((user_id, conversation_id))
            return item.model_copy(deep=True) if item else None

    async def list(self, limit: int, *, user_id: str) -> list[ConversationSummary]:
        async with self._lock:
            items = sorted(
                (item for (owner, _), item in self._items.items() if owner == user_id),
                key=lambda item: item.updated_at,
                reverse=True,
            )[:limit]
            return [ConversationSummary(**item.model_dump()) for item in items]

    async def get_map_context(
        self,
        conversation_id: str,
        *,
        user_id: str,
    ) -> Optional[FrontendContext]:
        async with self._lock:
            key = (user_id, conversation_id)
            if key not in self._items:
                return None
            context = self._map_contexts.get(key)
            return context.model_copy(deep=True) if context is not None else None

    async def save_map_context(
        self,
        conversation_id: str,
        context: FrontendContext,
        *,
        user_id: str,
    ) -> None:
        async with self._lock:
            key = (user_id, conversation_id)
            if key not in self._items:
                raise KeyError(conversation_id)
            self._map_contexts[key] = context.model_copy(deep=True)

    async def save_exchange(
        self,
        conversation_id: str,
        question: str,
        answer: str,
        sources: list[dict],
        *,
        user_id: str,
    ) -> ConversationMessage:
        _, assistant = await self.begin_exchange(
            conversation_id, question, run_id=None, user_id=user_id
        )
        return await self.update_assistant_message(
            assistant.id,
            answer,
            sources,
            status="completed",
            user_id=user_id,
        )

    async def begin_exchange(
        self,
        conversation_id: str,
        question: str,
        *,
        run_id: Optional[str],
        user_id: str,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        now = _now()
        user = ConversationMessage(
            id=uuid4().hex,
            role="user",
            content=question,
            created_at=now,
            updated_at=now,
            run_id=run_id,
        )
        assistant = ConversationMessage(
            id=uuid4().hex,
            role="assistant",
            content="",
            created_at=now,
            updated_at=now,
            run_id=run_id,
            status="streaming",
        )
        async with self._lock:
            item = self._items.get((user_id, conversation_id))
            if item is None:
                raise KeyError(conversation_id)
            item.messages.extend((user, assistant))
            item.message_count = len(item.messages)
            item.updated_at = now
            if item.title == "新会话":
                item.title = question[:30]
        return user.model_copy(deep=True), assistant.model_copy(deep=True)

    async def update_assistant_message(
        self,
        message_id: str,
        content: str,
        sources: list[dict],
        *,
        status: str,
        user_id: str,
        error_code: Optional[str] = None,
    ) -> ConversationMessage:
        now = _now()
        async with self._lock:
            for (owner, _), item in self._items.items():
                if owner != user_id:
                    continue
                for index, message in enumerate(item.messages):
                    if message.id == message_id and message.role == "assistant":
                        updated = message.model_copy(
                            update={
                                "content": content,
                                "sources": sources,
                                "status": status,
                                "error_code": error_code,
                                "updated_at": now,
                            },
                            deep=True,
                        )
                        item.messages[index] = updated
                        item.updated_at = now
                        return updated.model_copy(deep=True)
        raise KeyError(message_id)


conversation_repository = MemoryConversationRepository()
