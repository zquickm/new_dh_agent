from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from database.models import (
    ConversationMessageModel,
    ConversationModel,
    ConversationRunModel,
    MessageFeedbackModel,
    ToolCallAuditModel,
)
from database.runtime import DatabaseRuntime
from schemas.feedback import (
    AdminContextMessage,
    AdminFeedbackDetail,
    AdminFeedbackItem,
    AdminFeedbackPage,
    AdminRunSummary,
    AdminToolCallSummary,
    MessageFeedback,
)


class InvalidFeedbackTarget(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _optional_iso(value: Optional[datetime]) -> Optional[str]:
    return _iso(value) if value is not None else None


def _feedback_schema(item: MessageFeedbackModel) -> MessageFeedback:
    return MessageFeedback(
        id=item.id,
        feedback_type=item.feedback_type,
        comment=item.comment,
        status=item.status,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
    )


def _preview(value: str, limit: int = 180) -> str:
    normalized = " ".join((value or "").split())
    return normalized if len(normalized) <= limit else normalized[:limit] + "…"


class MySQLFeedbackRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self._runtime = runtime

    async def submit(
        self,
        conversation_id: str,
        message_id: str,
        *,
        feedback_type: str,
        comment: Optional[str],
        user_id: str,
        username: str,
    ) -> MessageFeedback:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                message = (
                    await session.execute(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.id == message_id,
                            ConversationMessageModel.conversation_id == conversation_id,
                            ConversationMessageModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if message is None:
                    raise KeyError(message_id)
                if message.role != "assistant":
                    raise InvalidFeedbackTarget("只能评价助手回复")
                if message.status == "streaming":
                    raise InvalidFeedbackTarget("回复生成完成后才能评价")

                item = (
                    await session.execute(
                        select(MessageFeedbackModel)
                        .where(MessageFeedbackModel.message_id == message_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if item is None:
                    item = MessageFeedbackModel(
                        id=uuid4().hex,
                        message_id=message_id,
                        feedback_type=feedback_type,
                        comment=comment,
                        status="ACTIVE",
                        username_snapshot=username,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(item)
                else:
                    item.feedback_type = feedback_type
                    item.comment = comment
                    item.status = "ACTIVE"
                    item.username_snapshot = username
                    item.updated_at = now
        return _feedback_schema(item)

    async def withdraw(
        self,
        conversation_id: str,
        message_id: str,
        *,
        user_id: str,
    ) -> Optional[MessageFeedback]:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                message = (
                    await session.execute(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.id == message_id,
                            ConversationMessageModel.conversation_id == conversation_id,
                            ConversationMessageModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if message is None:
                    raise KeyError(message_id)
                item = (
                    await session.execute(
                        select(MessageFeedbackModel)
                        .where(MessageFeedbackModel.message_id == message_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if item is not None and item.status != "WITHDRAWN":
                    item.status = "WITHDRAWN"
                    item.updated_at = now
        return _feedback_schema(item) if item is not None else None

    @staticmethod
    def _admin_query(user_message, *entities):
        return (
            select(*entities)
            .select_from(MessageFeedbackModel)
            .join(
                ConversationMessageModel,
                ConversationMessageModel.id == MessageFeedbackModel.message_id,
            )
            .join(
                ConversationModel,
                ConversationModel.id == ConversationMessageModel.conversation_id,
            )
            .outerjoin(
                ConversationRunModel,
                ConversationRunModel.id == ConversationMessageModel.run_id,
            )
            .outerjoin(
                user_message,
                user_message.id == ConversationRunModel.user_message_id,
            )
        )

    @staticmethod
    def _apply_admin_filters(
        query,
        user_message,
        *,
        feedback_type: Optional[str],
        status: Optional[str],
        user_id: Optional[int],
        keyword: Optional[str],
        start_at: Optional[datetime],
        end_at: Optional[datetime],
    ):
        if feedback_type is not None:
            query = query.where(MessageFeedbackModel.feedback_type == feedback_type)
        if status is not None:
            query = query.where(MessageFeedbackModel.status == status)
        if user_id is not None:
            query = query.where(ConversationMessageModel.user_id == user_id)
        if keyword:
            pattern = f"%{keyword}%"
            query = query.where(
                or_(
                    MessageFeedbackModel.comment.like(pattern),
                    MessageFeedbackModel.username_snapshot.like(pattern),
                    ConversationModel.title.like(pattern),
                    ConversationMessageModel.content.like(pattern),
                    user_message.content.like(pattern),
                )
            )
        if start_at is not None:
            query = query.where(MessageFeedbackModel.updated_at >= start_at)
        if end_at is not None:
            query = query.where(MessageFeedbackModel.updated_at <= end_at)
        return query

    async def list_admin(
        self,
        *,
        page: int,
        page_size: int,
        feedback_type: Optional[str],
        status: Optional[str],
        user_id: Optional[int],
        keyword: Optional[str],
        start_at: Optional[datetime],
        end_at: Optional[datetime],
    ) -> AdminFeedbackPage:
        user_message = aliased(ConversationMessageModel)
        entities = (
            MessageFeedbackModel,
            ConversationMessageModel,
            ConversationModel,
            ConversationRunModel,
            user_message,
        )
        query = self._admin_query(user_message, *entities)
        query = self._apply_admin_filters(
            query,
            user_message,
            feedback_type=feedback_type,
            status=status,
            user_id=user_id,
            keyword=keyword,
            start_at=start_at,
            end_at=end_at,
        )
        count_source = self._admin_query(user_message, MessageFeedbackModel.id)
        count_source = self._apply_admin_filters(
            count_source,
            user_message,
            feedback_type=feedback_type,
            status=status,
            user_id=user_id,
            keyword=keyword,
            start_at=start_at,
            end_at=end_at,
        )
        async with self._runtime.session_factory() as session:
            total = (
                await session.execute(
                    select(func.count()).select_from(count_source.subquery())
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    query.order_by(
                        MessageFeedbackModel.updated_at.desc(),
                        MessageFeedbackModel.id.desc(),
                    )
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        return AdminFeedbackPage(
            total=total,
            page=page,
            page_size=page_size,
            items=[self._admin_item(*row) for row in rows],
        )

    @staticmethod
    def _admin_item(
        feedback: MessageFeedbackModel,
        assistant: ConversationMessageModel,
        conversation: ConversationModel,
        run: Optional[ConversationRunModel],
        user_message: Optional[ConversationMessageModel],
    ) -> AdminFeedbackItem:
        return AdminFeedbackItem(
            id=feedback.id,
            feedback_type=feedback.feedback_type,
            comment=feedback.comment,
            status=feedback.status,
            user_id=assistant.user_id,
            username=feedback.username_snapshot,
            conversation_id=conversation.id,
            conversation_title=conversation.title,
            message_id=assistant.id,
            run_id=assistant.run_id,
            question_preview=_preview(user_message.content if user_message else ""),
            answer_preview=_preview(assistant.content),
            model=run.model if run else None,
            system_prompt_version_no=(run.system_prompt_version_no if run else None),
            created_at=_iso(feedback.created_at),
            updated_at=_iso(feedback.updated_at),
        )

    async def get_admin_detail(
        self,
        feedback_id: str,
        *,
        before_turns: int,
        after_turns: int,
    ) -> Optional[AdminFeedbackDetail]:
        user_message = aliased(ConversationMessageModel)
        query = self._admin_query(
            user_message,
            MessageFeedbackModel,
            ConversationMessageModel,
            ConversationModel,
            ConversationRunModel,
            user_message,
        ).where(MessageFeedbackModel.id == feedback_id)
        async with self._runtime.session_factory() as session:
            row = (await session.execute(query)).one_or_none()
            if row is None:
                return None
            feedback, assistant, conversation, run, question = row
            lower = max(1, assistant.sequence_no - before_turns * 2)
            upper = assistant.sequence_no + after_turns * 2
            context_messages = (
                await session.execute(
                    select(ConversationMessageModel)
                    .where(
                        ConversationMessageModel.conversation_id == conversation.id,
                        ConversationMessageModel.user_id == assistant.user_id,
                        ConversationMessageModel.sequence_no.between(lower, upper),
                    )
                    .order_by(ConversationMessageModel.sequence_no)
                )
            ).scalars().all()
            tool_calls = []
            if run is not None:
                tool_calls = (
                    await session.execute(
                        select(ToolCallAuditModel)
                        .where(ToolCallAuditModel.run_id == run.id)
                        .order_by(ToolCallAuditModel.sequence_no)
                        .limit(100)
                    )
                ).scalars().all()

        item = self._admin_item(feedback, assistant, conversation, run, question)
        run_summary = None
        if run is not None:
            run_summary = AdminRunSummary(
                id=run.id,
                status=run.status,
                model=run.model,
                system_prompt_version_no=run.system_prompt_version_no,
                system_prompt_sha256=run.system_prompt_sha256,
                started_at=_iso(run.started_at),
                finished_at=_optional_iso(run.finished_at),
                duration_ms=run.duration_ms,
                error_code=run.error_code,
                error_type=run.error_type,
                error_message=run.error_message,
            )
        return AdminFeedbackDetail(
            **item.model_dump(),
            context_messages=[
                AdminContextMessage(
                    id=message.id,
                    sequence_no=message.sequence_no,
                    role=message.role,
                    content=message.content,
                    status=message.status,
                    error_code=message.error_code,
                    run_id=message.run_id,
                    sources=list(message.sources or []),
                    images=list(message.images or []),
                    created_at=_iso(message.created_at),
                    target=message.id == assistant.id,
                )
                for message in context_messages
            ],
            run=run_summary,
            tool_calls=[
                AdminToolCallSummary(
                    tool_call_id=tool_call.tool_call_id,
                    sequence_no=tool_call.sequence_no,
                    tool_name=tool_call.tool_name,
                    status=tool_call.status,
                    duration_ms=tool_call.duration_ms,
                    error_code=tool_call.error_code,
                    error_type=tool_call.error_type,
                )
                for tool_call in tool_calls
            ],
        )
