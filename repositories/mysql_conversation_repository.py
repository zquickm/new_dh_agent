from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.dialects.mysql import insert as mysql_insert

from database.models import (
    ConversationMessageModel,
    ConversationModel,
    ConversationRagBufferModel,
    ConversationRunModel,
    MessageFeedbackModel,
    RunEventModel,
    ToolCallAuditModel,
)
from database.runtime import DatabaseRuntime
from schemas.chat import FrontendContext
from schemas.conversation import (
    ConversationDetail,
    ConversationMessage,
    ConversationRun,
    ConversationSummary,
    ContextUsage,
    RunStreamEvent,
    ToolCallAudit,
)
from schemas.feedback import MessageFeedback
from schemas.citation import RagBufferEntry
from schemas.reasoning import empty_reasoning, finish_reasoning


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


def _rag_buffer_schema(item: ConversationRagBufferModel) -> RagBufferEntry:
    return RagBufferEntry(
        buffer_id=item.id,
        identity_hash=item.identity_hash,
        text=item.text,
        source=item.source,
        score=item.score,
        document_id=item.document_id,
        chunk_id=item.chunk_id,
        knowledge_id=item.knowledge_id,
        title=item.title,
        url=item.url,
        page_index=item.page_index,
        line=item.line,
        page=item.page,
    )


def _message_schema(
    item: ConversationMessageModel,
    feedback: Optional[MessageFeedbackModel] = None,
) -> ConversationMessage:
    return ConversationMessage(
        id=item.id,
        role=item.role,
        content=item.content,
        model_messages=item.model_messages,
        reasoning=finish_reasoning(item.reasoning_json or empty_reasoning(), item.status) if item.status != "streaming" else item.reasoning_json or empty_reasoning(),
        reasoning_event_id=item.reasoning_event_sequence or 0,
        answer_origin=item.answer_origin or "model",
        exact_qa=item.exact_qa_json,
        created_at=_iso(item.created_at),
        sources=list(item.sources or []),
        images=list(item.images or []),
        recommend_questions=list(item.recommend_questions or []),
        run_id=item.run_id,
        status=item.status,
        stream_event_id=item.stream_event_sequence or 0,
        error_code=item.error_code,
        updated_at=_iso(item.updated_at),
        feedback=(
            MessageFeedback(
                id=feedback.id,
                feedback_type=feedback.feedback_type,
                comment=feedback.comment,
                status=feedback.status,
                created_at=_iso(feedback.created_at),
                updated_at=_iso(feedback.updated_at),
            )
            if feedback is not None
            else None
        ),
    )


def _summary_schema(
    item: ConversationModel, *, is_generating: bool = False
) -> ConversationSummary:
    return ConversationSummary(
        id=item.id,
        title=item.title,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
        message_count=item.message_count,
        is_generating=is_generating,
    )


def _run_schema(item: ConversationRunModel) -> ConversationRun:
    return ConversationRun(
        id=item.id,
        conversation_id=item.conversation_id,
        user_message_id=item.user_message_id,
        assistant_message_id=item.assistant_message_id,
        status=item.status,
        model=item.model,
        system_prompt_version_id=item.system_prompt_version_id,
        system_prompt_version_no=item.system_prompt_version_no,
        system_prompt_sha256=item.system_prompt_sha256,
        context_window_tokens=item.context_window_tokens,
        input_budget_tokens=item.input_budget_tokens,
        estimated_input_tokens=item.estimated_input_tokens,
        last_prompt_tokens=item.last_prompt_tokens,
        last_completion_tokens=item.last_completion_tokens,
        prompt_tokens=item.prompt_tokens or 0,
        completion_tokens=item.completion_tokens or 0,
        total_tokens=item.total_tokens or 0,
        history_messages_included=item.history_messages_included or 0,
        history_messages_dropped=item.history_messages_dropped or 0,
        tool_results_truncated=item.tool_results_truncated or 0,
        tokenizer_name=item.tokenizer_name,
        token_usage_source=item.token_usage_source,
        token_round_number=item.token_round_number or 0,
        last_event_id=item.last_event_sequence or 0,
        started_at=_iso(item.started_at),
        finished_at=_optional_iso(item.finished_at),
        duration_ms=item.duration_ms,
        heartbeat_at=_iso(item.heartbeat_at),
        cancel_requested_at=_optional_iso(item.cancel_requested_at),
        cancelled_at=_optional_iso(item.cancelled_at),
        cancel_reason=item.cancel_reason,
        error_code=item.error_code,
        error_type=item.error_type,
        error_message=item.error_message,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
    )


def _context_usage_schema(item: Optional[ConversationRunModel]) -> ContextUsage:
    if item is None or item.context_window_tokens is None:
        return ContextUsage()
    input_tokens = item.last_prompt_tokens or item.estimated_input_tokens or 0
    output_tokens = item.last_completion_tokens or 0
    used_tokens = input_tokens + output_tokens
    return ContextUsage(
        context_window_tokens=item.context_window_tokens,
        input_budget_tokens=item.input_budget_tokens or 0,
        estimated_input_tokens=item.estimated_input_tokens or 0,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        used_tokens=used_tokens,
        usage_percent=round(used_tokens * 100 / item.context_window_tokens, 2),
        history_messages_included=item.history_messages_included or 0,
        history_messages_dropped=item.history_messages_dropped or 0,
        tool_results_truncated=item.tool_results_truncated or 0,
        tokenizer=item.tokenizer_name or "deepseek-v4-official",
        round_number=item.token_round_number or 0,
        source=item.token_usage_source or "estimated",
        cumulative_input_tokens=item.prompt_tokens or 0,
        cumulative_output_tokens=item.completion_tokens or 0,
        cumulative_total_tokens=item.total_tokens or 0,
    )


def _tool_call_schema(item: ToolCallAuditModel) -> ToolCallAudit:
    return ToolCallAudit(
        id=item.id,
        run_id=item.run_id,
        tool_call_id=item.tool_call_id,
        round_number=item.round_number,
        sequence_no=item.sequence_no,
        tool_name=item.tool_name,
        status=item.status,
        arguments=dict(item.arguments or {}),
        arguments_truncated=item.arguments_truncated,
        result_summary=item.result_summary,
        result_size_bytes=item.result_size_bytes,
        result_sha256=item.result_sha256,
        result_truncated=item.result_truncated,
        started_at=_iso(item.started_at),
        finished_at=_optional_iso(item.finished_at),
        duration_ms=item.duration_ms,
        error_code=item.error_code,
        error_type=item.error_type,
        error_message=item.error_message,
        failure_stage=item.failure_stage,
        retryable=item.retryable,
        created_at=_iso(item.created_at),
        updated_at=_iso(item.updated_at),
    )


def _run_event_schema(item: RunEventModel) -> RunStreamEvent:
    return RunStreamEvent(
        id=item.sequence_no,
        event=item.event_type,
        data=dict(item.payload or {}),
    )


class MySQLConversationRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self._runtime = runtime

    async def list_rag_buffer(
        self, conversation_id: str, *, user_id: str
    ) -> list[RagBufferEntry]:
        async with self._runtime.session_factory() as session:
            items = (
                await session.execute(
                    select(ConversationRagBufferModel)
                    .where(
                        ConversationRagBufferModel.conversation_id == conversation_id,
                        ConversationRagBufferModel.user_id == int(user_id),
                    )
                    .order_by(ConversationRagBufferModel.id)
                )
            ).scalars().all()
            return [_rag_buffer_schema(item) for item in items]

    async def upsert_rag_buffer(
        self,
        conversation_id: str,
        entries: list[dict],
        *,
        user_id: str,
        query: str,
        run_id: str,
    ) -> list[RagBufferEntry]:
        if not entries:
            return []
        owner_id = int(user_id)
        now = _now()
        hashes = [str(item["identity_hash"]) for item in entries]
        async with self._runtime.session_factory() as session:
            async with session.begin():
                owns_conversation = await session.scalar(
                    select(exists().where(
                        ConversationModel.id == conversation_id,
                        ConversationModel.user_id == owner_id,
                    ))
                )
                if not owns_conversation:
                    raise KeyError(conversation_id)
                for item in entries:
                    values = {
                        "conversation_id": conversation_id,
                        "user_id": owner_id,
                        "identity_hash": item["identity_hash"],
                        "knowledge_id": item.get("knowledge_id"),
                        "document_id": item.get("document_id"),
                        "chunk_id": item.get("chunk_id"),
                        "source": item["source"],
                        "title": item.get("title"),
                        "text": item["text"],
                        "url": item.get("url"),
                        "page_index": item.get("page_index"),
                        "line": item.get("line"),
                        "page": item.get("page"),
                        "score": float(item.get("score") or 0),
                        "first_query": query,
                        "created_run_id": run_id,
                        "created_at": now,
                        "last_seen_at": now,
                    }
                    statement = mysql_insert(ConversationRagBufferModel).values(**values)
                    await session.execute(statement.on_duplicate_key_update(
                        last_seen_at=now,
                    ))
                stored = (
                    await session.execute(
                        select(ConversationRagBufferModel).where(
                            ConversationRagBufferModel.conversation_id == conversation_id,
                            ConversationRagBufferModel.user_id == owner_id,
                            ConversationRagBufferModel.identity_hash.in_(hashes),
                        )
                    )
                ).scalars().all()
        by_hash = {item.identity_hash: _rag_buffer_schema(item) for item in stored}
        return [by_hash[value] for value in hashes]

    async def create(self, title: str, *, user_id: str, identity=None) -> ConversationDetail:
        now = _now()
        item = ConversationModel(
            id=uuid4().hex,
            user_id=int(user_id),
            data_unit_id=int(identity.unit_id) if identity is not None else 1,
            data_department_id=int(identity.department_id) if identity is not None else 1,
            creator_display_name=(identity.display_name or None) if identity is not None else None,
            creator_role_codes=list(identity.role_codes) if identity is not None else ["GENERAL_USER"],
            creator_profile_version=identity.profile_version if identity is not None else 1,
            title=title.strip() or "新会话",
            context_enabled=True,
            context_message_limit=1000,
            context_token_limit=131072,
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
            feedback_by_message = {}
            message_ids = [message.id for message in messages]
            if message_ids:
                feedback_items = (
                    await session.execute(
                        select(MessageFeedbackModel).where(
                            MessageFeedbackModel.message_id.in_(message_ids),
                            MessageFeedbackModel.status == "ACTIVE",
                        )
                    )
                ).scalars().all()
                feedback_by_message = {
                    feedback.message_id: feedback for feedback in feedback_items
                }
            latest_run = (
                await session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.conversation_id == conversation_id,
                        ConversationRunModel.user_id == owner_id,
                    )
                    .order_by(ConversationRunModel.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return ConversationDetail(
                **_summary_schema(
                    item,
                    is_generating=(
                        latest_run is not None
                        and latest_run.status in {"running", "cancel_requested"}
                    ),
                ).model_dump(),
                context_enabled=item.context_enabled,
                context_message_limit=item.context_message_limit,
                context_token_limit=item.context_token_limit,
                context_usage=_context_usage_schema(latest_run),
                messages=[
                    _message_schema(message, feedback_by_message.get(message.id))
                    for message in messages
                ],
            )

    async def list(self, limit: int, *, user_id: str) -> list[ConversationSummary]:
        owner_id = int(user_id)
        active_run_exists = (
            exists()
            .where(
                ConversationRunModel.conversation_id == ConversationModel.id,
                ConversationRunModel.user_id == owner_id,
                ConversationRunModel.status.in_(("running", "cancel_requested")),
            )
            .correlate(ConversationModel)
        )
        async with self._runtime.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        ConversationModel,
                        active_run_exists.label("is_generating"),
                    )
                    .where(ConversationModel.user_id == owner_id)
                    .order_by(
                        ConversationModel.updated_at.desc(),
                        ConversationModel.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
            return [
                _summary_schema(item, is_generating=bool(is_generating))
                for item, is_generating in rows
            ]

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

    async def request_run_cancellation(
        self, conversation_id: str, run_id: str, *, user_id: str
    ) -> Optional[ConversationRun]:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                run = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.conversation_id == conversation_id,
                            ConversationRunModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    return None
                if run.status == "running":
                    run.status = "cancel_requested"
                    run.cancel_requested_at = now
                    run.cancel_reason = "user_requested"
                    run.updated_at = now
            return _run_schema(run)

    async def finalize_run_cancellation(
        self,
        run_id: str,
        *,
        user_id: str,
        answer: str,
        context_usage: dict,
        stream_event_sequence: int,
    ) -> Optional[tuple[ConversationMessage, RunStreamEvent]]:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                initial_run = (
                    await session.execute(
                        select(ConversationRunModel).where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.user_id == int(user_id),
                        )
                    )
                ).scalar_one_or_none()
                if initial_run is None:
                    return None
                message = (
                    await session.execute(
                        select(ConversationMessageModel)
                        .where(
                            ConversationMessageModel.id == initial_run.assistant_message_id,
                            ConversationMessageModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                run = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None or message is None or run.status != "cancel_requested":
                    return None

                message.content = answer
                message.model_messages = None
                message.sources = []
                message.status = "cancelled"
                message.reasoning_json = finish_reasoning(message.reasoning_json or empty_reasoning(), "cancelled")
                message.stream_event_sequence = max(
                    message.stream_event_sequence or 0, stream_event_sequence
                )
                message.error_code = "USER_CANCELLED"
                message.updated_at = now

                run.status = "cancelled"
                run.cancelled_at = now
                run.finished_at = now
                run.duration_ms = _duration_ms(run.started_at, now)
                run.heartbeat_at = now
                run.error_code = "USER_CANCELLED"
                run.error_type = "UserCancelled"
                run.error_message = "用户手动停止生成"
                run.updated_at = now
                await self._interrupt_running_tool_calls(
                    session,
                    run.id,
                    now,
                    error_code="USER_CANCELLED",
                    error_type="UserCancelled",
                    error_message="用户停止生成导致工具调用未完成",
                    retryable=False,
                )

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

                run.last_event_sequence = (run.last_event_sequence or 0) + 1
                event = RunEventModel(
                    run_id=run.id,
                    sequence_no=run.last_event_sequence,
                    event_type="cancelled",
                    payload={
                        "conversation_id": run.conversation_id,
                        "run_id": run.id,
                        "assistant_message_id": message.id,
                        "answer": answer,
                        "reasoning": message.reasoning_json,
                        "answer_origin": message.answer_origin,
                        "exact_qa": message.exact_qa_json,
                        "reason": "user_requested",
                        "context_usage": context_usage,
                    },
                    created_at=now,
                )
                session.add(event)
            return _message_schema(message), _run_event_schema(event)

    async def append_run_event(
        self,
        run_id: str,
        event_type: str,
        data: dict,
        *,
        user_id: str,
    ) -> RunStreamEvent:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                run = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    raise KeyError(run_id)
                run.last_event_sequence = (run.last_event_sequence or 0) + 1
                run.updated_at = now
                item = RunEventModel(
                    run_id=run_id,
                    sequence_no=run.last_event_sequence,
                    event_type=event_type,
                    payload=data,
                    created_at=now,
                )
                session.add(item)
        return _run_event_schema(item)

    async def list_run_events(
        self,
        conversation_id: str,
        run_id: str,
        *,
        after: int,
        user_id: str,
        limit: int = 200,
    ) -> tuple[list[RunStreamEvent], Optional[str]]:
        async with self._runtime.session_factory() as session:
            run = (
                await session.execute(
                    select(ConversationRunModel).where(
                        ConversationRunModel.id == run_id,
                        ConversationRunModel.conversation_id == conversation_id,
                        ConversationRunModel.user_id == int(user_id),
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                return [], None
            items = (
                await session.execute(
                    select(RunEventModel)
                    .where(
                        RunEventModel.run_id == run_id,
                        RunEventModel.sequence_no > after,
                        RunEventModel.event_type.notin_(("rag_debug", "rag_debug_part")),
                    )
                    .order_by(RunEventModel.sequence_no)
                    .limit(limit)
                )
            ).scalars().all()
            return [_run_event_schema(item) for item in items], run.status

    async def list_tool_calls(
        self,
        conversation_id: str,
        run_id: str,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[ToolCallAudit]:
        async with self._runtime.session_factory() as session:
            items = (
                await session.execute(
                    select(ToolCallAuditModel)
                    .join(
                        ConversationRunModel,
                        ConversationRunModel.id == ToolCallAuditModel.run_id,
                    )
                    .where(
                        ToolCallAuditModel.run_id == run_id,
                        ConversationRunModel.conversation_id == conversation_id,
                        ConversationRunModel.user_id == int(user_id),
                    )
                    .order_by(ToolCallAuditModel.sequence_no)
                    .limit(limit)
                )
            ).scalars().all()
            return [_tool_call_schema(item) for item in items]

    async def get_tool_call(
        self,
        conversation_id: str,
        run_id: str,
        tool_call_id: str,
        *,
        user_id: str,
    ) -> Optional[ToolCallAudit]:
        async with self._runtime.session_factory() as session:
            item = (
                await session.execute(
                    select(ToolCallAuditModel)
                    .join(
                        ConversationRunModel,
                        ConversationRunModel.id == ToolCallAuditModel.run_id,
                    )
                    .where(
                        ToolCallAuditModel.run_id == run_id,
                        ToolCallAuditModel.tool_call_id == tool_call_id,
                        ConversationRunModel.conversation_id == conversation_id,
                        ConversationRunModel.user_id == int(user_id),
                    )
                )
            ).scalar_one_or_none()
            return _tool_call_schema(item) if item is not None else None

    async def begin_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        round_number: int,
        sequence_no: int,
        tool_name: str,
        arguments: dict,
        arguments_truncated: bool,
    ) -> ToolCallAudit:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                run = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.status == "running",
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    raise KeyError(run_id)
                item = ToolCallAuditModel(
                    id=uuid4().hex,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    round_number=round_number,
                    sequence_no=sequence_no,
                    tool_name=tool_name,
                    status="running",
                    arguments=arguments,
                    arguments_truncated=arguments_truncated,
                    result_summary=None,
                    result_size_bytes=None,
                    result_sha256=None,
                    result_truncated=False,
                    started_at=now,
                    finished_at=None,
                    duration_ms=None,
                    error_code=None,
                    error_type=None,
                    error_message=None,
                    failure_stage=None,
                    retryable=False,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
        return _tool_call_schema(item)

    async def finish_tool_call(
        self,
        run_id: str,
        *,
        tool_call_id: str,
        status: str,
        result_summary: str,
        result_size_bytes: int,
        result_sha256: str,
        result_truncated: bool,
        duration_ms: int,
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        failure_stage: Optional[str] = None,
        retryable: bool = False,
    ) -> ToolCallAudit:
        if status not in {"succeeded", "failed", "interrupted"}:
            raise ValueError(f"invalid tool call status: {status}")
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                item = (
                    await session.execute(
                        select(ToolCallAuditModel)
                        .where(
                            ToolCallAuditModel.run_id == run_id,
                            ToolCallAuditModel.tool_call_id == tool_call_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if item is None:
                    raise KeyError(tool_call_id)
                if item.status != "running":
                    raise RuntimeError(
                        f"tool call already finalized: {tool_call_id}"
                    )
                item.status = status
                item.result_summary = result_summary
                item.result_size_bytes = result_size_bytes
                item.result_sha256 = result_sha256
                item.result_truncated = result_truncated
                item.finished_at = now
                item.duration_ms = max(0, duration_ms)
                item.error_code = error_code
                item.error_type = error_type
                item.error_message = error_message
                item.failure_stage = failure_stage
                item.retryable = retryable
                item.updated_at = now
        return _tool_call_schema(item)

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
        system_prompt_version_id: Optional[int] = None,
        system_prompt_version_no: Optional[int] = None,
        system_prompt_sha256: Optional[str] = None,
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
                    stream_event_sequence=0,
                    reasoning_event_sequence=0,
                    answer_origin="model",
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
                    stream_event_sequence=0,
                    reasoning_event_sequence=0,
                    answer_origin="model",
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
                    system_prompt_version_id=system_prompt_version_id,
                    system_prompt_version_no=system_prompt_version_no,
                    system_prompt_sha256=system_prompt_sha256,
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

    async def save_reasoning(self, message_id: str, reasoning: dict, *, event_sequence: int, user_id: str) -> None:
        async with self._runtime.session_factory() as session:
            async with session.begin():
                message = (await session.execute(select(ConversationMessageModel).where(
                    ConversationMessageModel.id == message_id,
                    ConversationMessageModel.user_id == int(user_id),
                    ConversationMessageModel.role == "assistant",
                ).with_for_update())).scalar_one_or_none()
                if message is None:
                    raise KeyError(message_id)
                message.reasoning_json = reasoning
                message.reasoning_event_sequence = event_sequence
                message.updated_at = _now()

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
        model_messages: Optional[list[dict]] = None,
        stream_event_sequence: Optional[int] = None,
        reasoning: Optional[dict] = None,
        answer_origin: Optional[str] = None,
        exact_qa: Optional[dict] = None,
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
                if reasoning is not None:
                    message.reasoning_json = reasoning
                if answer_origin is not None:
                    message.answer_origin = answer_origin
                if exact_qa is not None:
                    message.exact_qa_json = exact_qa
                if model_messages is not None:
                    message.model_messages = model_messages
                if stream_event_sequence is not None:
                    message.stream_event_sequence = stream_event_sequence
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
                        if run.status != "running":
                            raise RuntimeError(
                                f"run {run.id} is not active: {run.status}"
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
                            await self._interrupt_running_tool_calls(
                                session,
                                run.id,
                                now,
                                error_type=error_type or "RunFinalized",
                                error_message=(
                                    error_message
                                    or "运行结束时工具调用仍未完成"
                                ),
                            )
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

    async def update_run_token_usage(
        self, run_id: str, usage: dict, *, user_id: str
    ) -> None:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                run = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.id == run_id,
                            ConversationRunModel.user_id == int(user_id),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if run is None:
                    raise KeyError(run_id)
                run.context_window_tokens = int(usage["context_window_tokens"])
                run.input_budget_tokens = int(usage["input_budget_tokens"])
                run.estimated_input_tokens = int(usage["estimated_input_tokens"])
                run.last_prompt_tokens = int(usage.get("input_tokens") or 0)
                run.last_completion_tokens = int(usage.get("output_tokens") or 0)
                run.prompt_tokens = int(usage.get("cumulative_input_tokens") or 0)
                run.completion_tokens = int(usage.get("cumulative_output_tokens") or 0)
                run.total_tokens = int(usage.get("cumulative_total_tokens") or 0)
                run.history_messages_included = int(usage.get("history_messages_included") or 0)
                run.history_messages_dropped = int(usage.get("history_messages_dropped") or 0)
                run.tool_results_truncated = int(usage.get("tool_results_truncated") or 0)
                run.tokenizer_name = str(usage.get("tokenizer") or "deepseek-v4-official")
                run.token_usage_source = str(usage.get("source") or "estimated")
                run.token_round_number = int(usage.get("round_number") or 0)
                run.updated_at = now

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
                    await self._interrupt_running_tool_calls(
                        session,
                        run.id,
                        now,
                        error_type="StaleRunRecovery",
                        error_message="服务中断导致工具调用未完成",
                    )
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

    async def recover_cancel_requested_runs(self, stale_before: datetime) -> int:
        now = _now()
        recovered = 0
        async with self._runtime.session_factory() as session:
            async with session.begin():
                runs = (
                    await session.execute(
                        select(ConversationRunModel)
                        .where(
                            ConversationRunModel.status == "cancel_requested",
                            ConversationRunModel.cancel_requested_at < stale_before,
                        )
                        .with_for_update()
                    )
                ).scalars().all()
                for run in runs:
                    message = (
                        await session.execute(
                            select(ConversationMessageModel)
                            .where(
                                ConversationMessageModel.id == run.assistant_message_id,
                                ConversationMessageModel.user_id == run.user_id,
                            )
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if message is None:
                        continue
                    message.model_messages = None
                    message.status = "cancelled"
                    message.error_code = "USER_CANCELLED"
                    message.updated_at = now
                    run.status = "cancelled"
                    run.cancelled_at = now
                    run.finished_at = now
                    run.duration_ms = _duration_ms(run.started_at, now)
                    run.error_code = "USER_CANCELLED"
                    run.error_type = "CancelTimeoutRecovery"
                    run.error_message = "取消请求超时后由后台任务收敛"
                    run.updated_at = now
                    await self._interrupt_running_tool_calls(
                        session,
                        run.id,
                        now,
                        error_code="USER_CANCELLED",
                        error_type="CancelTimeoutRecovery",
                        error_message="用户停止生成导致工具调用未完成",
                        retryable=False,
                    )
                    run.last_event_sequence = (run.last_event_sequence or 0) + 1
                    session.add(RunEventModel(
                        run_id=run.id,
                        sequence_no=run.last_event_sequence,
                        event_type="cancelled",
                        payload={
                            "conversation_id": run.conversation_id,
                            "run_id": run.id,
                            "assistant_message_id": message.id,
                            "answer": message.content,
                            "reason": "user_requested",
                            "context_usage": {},
                        },
                        created_at=now,
                    ))
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

    @staticmethod
    async def _interrupt_running_tool_calls(
        session,
        run_id: str,
        now: datetime,
        *,
        error_code: str = "AGENT_INTERRUPTED",
        error_type: str,
        error_message: str,
        retryable: bool = True,
    ) -> None:
        items = (
            await session.execute(
                select(ToolCallAuditModel)
                .where(
                    ToolCallAuditModel.run_id == run_id,
                    ToolCallAuditModel.status == "running",
                )
                .with_for_update()
            )
        ).scalars().all()
        for item in items:
            item.status = "interrupted"
            item.finished_at = now
            item.duration_ms = _duration_ms(item.started_at, now)
            item.error_code = error_code
            item.error_type = error_type
            item.error_message = error_message
            item.failure_stage = "execution"
            item.retryable = retryable
            item.updated_at = now
