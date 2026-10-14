from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import load_only

from database.models import (
    ConversationMessageModel, ConversationModel, ConversationRunModel,
    RunEventModel, SystemPromptVersionModel, ToolCallAuditModel,
)
from database.runtime import DatabaseRuntime


def without(model, *excluded):
    return load_only(*[getattr(model, column.key) for column in model.__table__.columns
                       if column.key not in excluded])


class MySQLAdminConversationRepository:
    """Read-only admin queries against the Agent database."""

    def __init__(self, runtime: DatabaseRuntime):
        self._runtime = runtime

    async def list_conversations(self, *, page, page_size, user_id, keyword,
                                 start_at, end_at, time_field, sort_by, sort_order):
        columns = {"createdAt": ConversationModel.created_at,
                   "updatedAt": ConversationModel.updated_at, "userId": ConversationModel.user_id}
        conditions = []
        if user_id is not None:
            conditions.append(ConversationModel.user_id == user_id)
        if keyword:
            conditions.append(or_(
                ConversationModel.title.contains(keyword, autoescape=True),
                ConversationModel.creator_display_name.contains(keyword, autoescape=True),
            ))
        if start_at is not None:
            conditions.append(columns[time_field] >= start_at)
        if end_at is not None:
            conditions.append(columns[time_field] <= end_at)
        ordering = columns[sort_by].asc() if sort_order == "asc" else columns[sort_by].desc()
        async with self._runtime.session_factory() as session:
            total = await session.scalar(select(func.count()).select_from(ConversationModel).where(*conditions))
            items = (await session.execute(select(ConversationModel)
                .options(without(ConversationModel, "map_context")).where(*conditions)
                .order_by(ordering, ConversationModel.updated_at.desc(), ConversationModel.id.desc())
                .offset((page - 1) * page_size).limit(page_size))).scalars().all()
            return items, total

    async def get_conversation(self, conversation_id, *, page, page_size):
        async with self._runtime.session_factory() as session:
            conversation = await session.get(ConversationModel, conversation_id)
            if conversation is None:
                return None
            conditions = (ConversationMessageModel.conversation_id == conversation_id,
                          ConversationMessageModel.user_id == conversation.user_id)
            total = await session.scalar(select(func.count()).select_from(ConversationMessageModel).where(*conditions))
            messages = (await session.execute(select(ConversationMessageModel)
                .options(without(ConversationMessageModel, "model_messages")).where(*conditions)
                .order_by(ConversationMessageModel.sequence_no.asc())
                .offset((page - 1) * page_size).limit(page_size))).scalars().all()
            ids = [item.id for item in messages]
            runs = (await session.execute(select(ConversationRunModel).where(
                ConversationRunModel.conversation_id == conversation_id,
                ConversationRunModel.user_id == conversation.user_id,
                or_(ConversationRunModel.user_message_id.in_(ids), ConversationRunModel.assistant_message_id.in_(ids)),
            ).order_by(ConversationRunModel.started_at.asc(), ConversationRunModel.id.asc()))).scalars().all() if ids else []
            return conversation, messages, runs, total

    @staticmethod
    async def _run(session, conversation_id, run_id):
        return (await session.execute(select(ConversationRunModel).join(
            ConversationModel,
            (ConversationModel.id == ConversationRunModel.conversation_id)
            & (ConversationModel.user_id == ConversationRunModel.user_id),
        ).where(ConversationRunModel.id == run_id,
                ConversationRunModel.conversation_id == conversation_id))).scalar_one_or_none()

    async def get_run(self, conversation_id, run_id, *, page, page_size):
        async with self._runtime.session_factory() as session:
            run = await self._run(session, conversation_id, run_id)
            if run is None:
                return None
            message = (await session.execute(select(ConversationMessageModel).where(
                ConversationMessageModel.id == run.assistant_message_id,
                ConversationMessageModel.conversation_id == conversation_id,
                ConversationMessageModel.user_id == run.user_id,
            ))).scalar_one_or_none()
            total = await session.scalar(select(func.count()).select_from(ToolCallAuditModel).where(ToolCallAuditModel.run_id == run_id))
            tools = (await session.execute(select(ToolCallAuditModel).where(ToolCallAuditModel.run_id == run_id)
                .order_by(ToolCallAuditModel.sequence_no.asc())
                .offset((page - 1) * page_size).limit(page_size))).scalars().all()
            prompt = await session.get(SystemPromptVersionModel, run.system_prompt_version_id) if run.system_prompt_version_id else None
            return run, message.model_messages if message else None, prompt, tools, total

    async def get_events(self, conversation_id, run_id, *, after_sequence, page_size):
        async with self._runtime.session_factory() as session:
            if await self._run(session, conversation_id, run_id) is None:
                return None
            events = (await session.execute(select(RunEventModel).where(
                RunEventModel.run_id == run_id, RunEventModel.sequence_no > after_sequence,
            ).order_by(RunEventModel.sequence_no.asc()).limit(page_size + 1))).scalars().all()
            return events[:page_size], len(events) > page_size
