from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import select

from database.models import (
    ConversationMessageModel,
    ConversationModel,
    ConversationRunModel,
)
from database.runtime import DatabaseRuntime
from schemas.chat import FrontendContext
from schemas.conversation import (
    ConversationDetail,
    ConversationMessage,
    ConversationRun,
    ConversationSummary,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _optional_iso(value: Optional[datetime]) -> Optional[str]:
    return _iso(value) if value is not None else None


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


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


def _run_schema(item: ConversationRunModel) -> ConversationRun:
    return ConversationRun(
        id=item.id,
        conversation_id=item.conversation_id,
        user_message_id=item.user_message_id,
        assistant_message_id=item.assistant_message_id,
        status=item.status,
        model=item.model,
        started_at=_iso(item.started_at),
        finished_at=_optional_iso(item.finished_at),
        duration_ms=item.duration_ms,
        heartbeat_at=_iso(item.heartbeat_at),
        error_code=item.error_code,
        error_type=item.error_type,
        error_message=item.error_message,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
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

    async def get_run(
        self, conversation_id: str, run_id: str, *, user_id: str
    ) -> Optional[ConversationRun]:
        async with self._runtime.session_factory() as session:
            item = (
                await session.execute(
                    select(ConversationRunModel).where(
                        ConversationRunModel.id == run_id,
                        ConversationRunModel.conversation_id == conversation_id,
                        ConversationRunModel.user_id == int(user_id),
                    )
                )
            ).scalar_one_or_none()
            return _run_schema(item) if item is not None else None

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
        run_id: Optional[str] = None,
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
                # A late request must not overwrite a newer turn's selection.
                if run_id is not None:
                    latest_run_id = (
                        await session.execute(
                            select(ConversationMessageModel.run_id)
                            .where(
                                ConversationMessageModel.conversation_id == conversation_id,
                                ConversationMessageModel.user_id == int(user_id),
                                ConversationMessageModel.role == "user",
                            )
                            .order_by(ConversationMessageModel.sequence_no.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if latest_run_id != run_id:
                        return
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
            conversation_id,
            question,
            run_id=None,
            user_id=user_id,
            model="repository",
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
        model: Optional[str] = None,
        owner_instance_id: Optional[str] = None,
        assistant_content: str = "",
        assistant_status: str = "streaming",
        run_status: str = "running",
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> tuple[ConversationMessage, ConversationMessage]:
        owner_id = int(user_id)
        now = _now()
        effective_run_id = run_id or str(uuid4())
        user_message_id = uuid4().hex
        assistant_message_id = uuid4().hex
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
                    id=user_message_id,
                    conversation_id=conversation_id,
                    user_id=owner_id,
                    sequence_no=conversation.message_count + 1,
                    role="user",
                    content=question,
                    sources=[],
                    images=[],
                    recommend_questions=[],
                    run_id=effective_run_id,
                    status="completed",
                    error_code=None,
                    created_at=now,
                    updated_at=now,
                )
                assistant_message = ConversationMessageModel(
                    id=assistant_message_id,
                    conversation_id=conversation_id,
                    user_id=owner_id,
                    sequence_no=conversation.message_count + 2,
                    role="assistant",
                    content=assistant_content,
                    sources=[],
                    images=[],
                    recommend_questions=[],
                    run_id=effective_run_id,
                    status=assistant_status,
                    error_code=error_code,
                    created_at=now,
                    updated_at=now,
                )
                is_finished = run_status != "running"
                run = ConversationRunModel(
                    id=effective_run_id,
                    conversation_id=conversation_id,
                    user_id=owner_id,
                    user_message_id=user_message_id,
                    assistant_message_id=assistant_message_id,
                    status=run_status,
                    model=model,
                    owner_instance_id=owner_instance_id,
                    started_at=now,
                    finished_at=now if is_finished else None,
                    duration_ms=0 if is_finished else None,
                    heartbeat_at=now,
                    error_code=error_code,
                    error_type=error_type,
                    error_message=error_message,
                    created_at=now,
                    updated_at=now,
                )
                session.add_all((user_message, assistant_message, run))
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
        run_status: Optional[str] = None,
        model: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
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
                if message.run_id is not None:
                    run = (
                        await session.execute(
                            select(ConversationRunModel)
                            .where(
                                ConversationRunModel.id == message.run_id,
                                ConversationRunModel.user_id == int(user_id),
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if run is not None:
                        effective_run_status = run_status or (
                            "running" if status == "streaming" else status
                        )
                        run.status = effective_run_status
                        run.heartbeat_at = now
                        run.updated_at = now
                        if model is not None:
                            run.model = model
                        run.error_code = error_code
                        run.error_type = error_type
                        run.error_message = error_message
                        if effective_run_status != "running":
                            run.finished_at = now
                            run.duration_ms = _duration_ms(run.started_at, now)
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

    async def heartbeat_run(
        self,
        run_id: str,
        *,
        user_id: str,
        owner_instance_id: str,
    ) -> bool:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                run = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.user_id == int(user_id),
                            ConversationRunModel.owner_instance_id
                            == owner_instance_id,
                            ConversationRunModel.status == "running",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    return False
                run.heartbeat_at = now
                run.updated_at = now
                return True

    async def recover_stale_runs(self, stale_before: datetime) -> int:
        now = _now()
        recovered = 0
        async with self._runtime.session_factory() as session:
            async with session.begin():
                runs = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.status == "running",
                            ConversationRunModel.heartbeat_at < stale_before,
                        )
                        .with_for_update()
                    )
                ).scalars().all()
                for run in runs:
                    run.status = "interrupted"
                    run.finished_at = now
                    run.duration_ms = _duration_ms(run.started_at, now)
                    run.error_code = "AGENT_INTERRUPTED"
                    run.error_type = "StaleRunRecovery"
                    run.error_message = "服务中断导致运行心跳超时"
                    run.updated_at = now
                    message = (
                        await session.execute(
                            select(ConversationMessageModel)
                            .where(
                                ConversationMessageModel.id
                                == run.assistant_message_id,
                                ConversationMessageModel.user_id == run.user_id,
                                ConversationMessageModel.status == "streaming",
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if message is not None:
                        if not message.content.strip():
                            message.content = "服务中断导致回答未完成，请重新发送。"
                        message.status = "failed"
                        message.error_code = "AGENT_INTERRUPTED"
                        message.updated_at = now
                    conversation = (
                        await session.execute(
                            select(ConversationModel)
                            .where(
                                ConversationModel.id == run.conversation_id,
                                ConversationModel.user_id == run.user_id,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if conversation is not None:
                        conversation.updated_at = now
                        conversation.version += 1
                    recovered += 1
        return recovered
