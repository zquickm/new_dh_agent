from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import select

from database.models import ConversationMessageModel, ConversationModel
from database.runtime import DatabaseRuntime
from schemas.chat import FrontendContext
from schemas.conversation import (
    ConversationDetail,
    ConversationMessage,
    ConversationSummary,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _message_schema(item: ConversationMessageModel) -> ConversationMessage:
    return ConversationMessage(
        id=item.id,
        role=item.role,
        content=item.content,
        created_at=_iso(item.created_at),
        sources=list(item.sources or []),
        images=list(item.images or []),
        recommend_questions=list(item.recommend_questions or []),
        run_id=item.run_id,
        status=item.status,
        error_code=item.error_code,
        updated_at=_iso(item.updated_at),
    )


def _summary_schema(item: ConversationModel) -> ConversationSummary:
    return ConversationSummary(
        id=item.id,
        title=item.title,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
        message_count=item.message_count,
    )


class MySQLConversationRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self._runtime = runtime

    async def create(self, title: str, *, user_id: str) -> ConversationDetail:
        now = _now()
        item = ConversationModel(
            id=uuid4().hex,
            user_id=int(user_id),
            title=title.strip() or "新会话",
            context_enabled=True,
            context_message_limit=20,
            context_token_limit=None,
            map_context=None,
            message_count=0,
            version=0,
            created_at=now,
            updated_at=now,
        )
        async with self._runtime.session_factory() as session:
            async with session.begin():
                session.add(item)
        return ConversationDetail(**_summary_schema(item).model_dump())

    async def get(
        self, conversation_id: str, *, user_id: str
    ) -> Optional[ConversationDetail]:
        owner_id = int(user_id)
        async with self._runtime.session_factory() as session:
            item = (
                await session.execute(
                    select(ConversationModel).where(
                        ConversationModel.id == conversation_id,
                        ConversationModel.user_id == owner_id,
                    )
                )
            ).scalar_one_or_none()
            if item is None:
                return None
            messages = (
                await session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.conversation_id == conversation_id,
                        ConversationMessageModel.user_id == owner_id,
                    )
                    .order_by(ConversationMessageModel.sequence_no)
                )
            ).scalars().all()
            return ConversationDetail(
                **_summary_schema(item).model_dump(),
                context_enabled=item.context_enabled,
                context_message_limit=item.context_message_limit,
                context_token_limit=item.context_token_limit,
                messages=[_message_schema(message) for message in messages],
            )

    async def list(self, limit: int, *, user_id: str) -> list[ConversationSummary]:
        async with self._runtime.session_factory() as session:
            items = (
                await session.execute(
                    select(ConversationModel)
                    .where(ConversationModel.user_id == int(user_id))
                    .order_by(
                        ConversationModel.updated_at.desc(),
                        ConversationModel.id.desc(),
                    )
                    .limit(limit)
                )
            ).scalars().all()
            return [_summary_schema(item) for item in items]

    async def get_map_context(
        self, conversation_id: str, *, user_id: str
    ) -> Optional[FrontendContext]:
        async with self._runtime.session_factory() as session:
            context_value = (
                await session.execute(
                    select(ConversationModel.map_context).where(
                        ConversationModel.id == conversation_id,
                        ConversationModel.user_id == int(user_id),
                    )
                )
            ).scalar_one_or_none()
            return (
                FrontendContext.model_validate(context_value)
                if context_value is not None
                else None
            )

    async def save_map_context(
        self,
        conversation_id: str,
        context: FrontendContext,
        *,
        user_id: str,
    ) -> None:
        async with self._runtime.session_factory() as session:
            async with session.begin():
                item = (
                    await session.execute(
                        select(ConversationModel)
                        .where(
                            ConversationModel.id == conversation_id,
                            ConversationModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if item is None:
                    raise KeyError(conversation_id)
                item.map_context = context.model_dump(mode="json")
                item.version += 1

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
        owner_id = int(user_id)
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                conversation = (
                    await session.execute(
                        select(ConversationModel)
                        .where(
                            ConversationModel.id == conversation_id,
                            ConversationModel.user_id == owner_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if conversation is None:
                    raise KeyError(conversation_id)
                user_message = ConversationMessageModel(
                    id=uuid4().hex,
                    conversation_id=conversation_id,
                    user_id=owner_id,
                    sequence_no=conversation.message_count + 1,
                    role="user",
                    content=question,
                    sources=[],
                    images=[],
                    recommend_questions=[],
                    run_id=run_id,
                    status="completed",
                    error_code=None,
                    created_at=now,
                    updated_at=now,
                )
                assistant_message = ConversationMessageModel(
                    id=uuid4().hex,
                    conversation_id=conversation_id,
                    user_id=owner_id,
                    sequence_no=conversation.message_count + 2,
                    role="assistant",
                    content="",
                    sources=[],
                    images=[],
                    recommend_questions=[],
                    run_id=run_id,
                    status="streaming",
                    error_code=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add_all((user_message, assistant_message))
                conversation.message_count += 2
                conversation.updated_at = now
                conversation.version += 1
                if conversation.title == "新会话":
                    conversation.title = question[:30]
        return _message_schema(user_message), _message_schema(assistant_message)

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
        async with self._runtime.session_factory() as session:
            async with session.begin():
                message = (
                    await session.execute(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.id == message_id,
                            ConversationMessageModel.user_id == int(user_id),
                            ConversationMessageModel.role == "assistant",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if message is None:
                    raise KeyError(message_id)
                message.content = content
                message.sources = sources
                message.status = status
                message.error_code = error_code
                message.updated_at = now
                conversation = (
                    await session.execute(
                        select(ConversationModel)
                        .where(
                            ConversationModel.id == message.conversation_id,
                            ConversationModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one()
                conversation.updated_at = now
                conversation.version += 1
        return _message_schema(message)
