from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryConversationRepository:
    """In-memory fake used only by unit tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ConversationDetail] = {}
        self._map_contexts: dict[tuple[str, str], FrontendContext] = {}
        self._runs: dict[tuple[str, str], ConversationRun] = {}
        self._tool_calls: dict[tuple[str, str], ToolCallAudit] = {}
        self._run_events: dict[tuple[str, str], list[RunStreamEvent]] = {}
        self._lock = asyncio.Lock()

    async def create(self, title: str, *, user_id: str, identity=None) -> ConversationDetail:
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
            if item is None:
                return None
            copied = item.model_copy(deep=True)
            copied.is_generating = any(
                owner == user_id
                and run.conversation_id == conversation_id
                and run.status in {"running", "cancel_requested"}
                for (owner, _), run in self._runs.items()
            )
            return copied

    async def list(self, limit: int, *, user_id: str) -> list[ConversationSummary]:
        async with self._lock:
            items = sorted(
                (item for (owner, _), item in self._items.items() if owner == user_id),
                key=lambda item: item.updated_at,
                reverse=True,
            )[:limit]
            active_conversation_ids = {
                run.conversation_id
                for (owner, _), run in self._runs.items()
                if owner == user_id
                and run.status in {"running", "cancel_requested"}
            }
            return [
                ConversationSummary(
                    **item.model_dump(exclude={"is_generating"}),
                    is_generating=item.id in active_conversation_ids,
                )
                for item in items
            ]

    async def get_run(
        self, conversation_id: str, run_id: str, *, user_id: str
    ) -> Optional[ConversationRun]:
        async with self._lock:
            run = self._runs.get((user_id, run_id))
            if run is None or run.conversation_id != conversation_id:
                return None
            return run.model_copy(deep=True)

    async def request_run_cancellation(
        self, conversation_id: str, run_id: str, *, user_id: str
    ) -> Optional[ConversationRun]:
        async with self._lock:
            key = (user_id, run_id)
            run = self._runs.get(key)
            if run is None or run.conversation_id != conversation_id:
                return None
            if run.status == "running":
                now = _now()
                run = run.model_copy(update={
                    "status": "cancel_requested",
                    "cancel_requested_at": now,
                    "cancel_reason": "user_requested",
                    "updated_at": now,
                }, deep=True)
                self._runs[key] = run
            return run.model_copy(deep=True)

    async def finalize_run_cancellation(
        self,
        run_id: str,
        *,
        user_id: str,
        answer: str,
        context_usage: dict,
        stream_event_sequence: int,
    ) -> Optional[tuple[ConversationMessage, RunStreamEvent]]:
        async with self._lock:
            key = (user_id, run_id)
            run = self._runs.get(key)
            if run is None or run.status != "cancel_requested":
                return None
            now = _now()
            conversation = self._items.get((user_id, run.conversation_id))
            if conversation is None:
                return None
            message = next(
                (item for item in conversation.messages if item.id == run.assistant_message_id),
                None,
            )
            if message is None:
                return None
            updated_message = message.model_copy(update={
                "content": answer,
                "model_messages": None,
                "sources": [],
                "status": "cancelled",
                "stream_event_id": max(message.stream_event_id, stream_event_sequence),
                "error_code": "USER_CANCELLED",
                "updated_at": now,
            }, deep=True)
            conversation.messages = [
                updated_message if item.id == message.id else item
                for item in conversation.messages
            ]
            conversation.updated_at = now
            started_at = datetime.fromisoformat(run.started_at)
            finished_at = datetime.fromisoformat(now)
            sequence = run.last_event_id + 1
            self._runs[key] = run.model_copy(update={
                "status": "cancelled",
                "cancelled_at": now,
                "finished_at": now,
                "duration_ms": max(0, int((finished_at - started_at).total_seconds() * 1000)),
                "heartbeat_at": now,
                "error_code": "USER_CANCELLED",
                "error_type": "UserCancelled",
                "error_message": "用户手动停止生成",
                "last_event_id": sequence,
                "updated_at": now,
            }, deep=True)
            self._interrupt_tool_calls(
                run_id, now,
                error_code="USER_CANCELLED",
                error_type="UserCancelled",
                error_message="用户停止生成导致工具调用未完成",
                retryable=False,
            )
            event = RunStreamEvent(id=sequence, event="cancelled", data={
                "conversation_id": run.conversation_id,
                "run_id": run.id,
                "assistant_message_id": message.id,
                "answer": answer,
                "reason": "user_requested",
                "context_usage": context_usage,
            })
            self._run_events.setdefault(key, []).append(event)
            return updated_message.model_copy(deep=True), event.model_copy(deep=True)

    async def append_run_event(
        self,
        run_id: str,
        event_type: str,
        data: dict,
        *,
        user_id: str,
    ) -> RunStreamEvent:
        async with self._lock:
            key = (user_id, run_id)
            run = self._runs.get(key)
            if run is None:
                raise KeyError(run_id)
            sequence = run.last_event_id + 1
            event = RunStreamEvent(id=sequence, event=event_type, data=data)
            self._run_events.setdefault(key, []).append(event)
            self._runs[key] = run.model_copy(
                update={"last_event_id": sequence, "updated_at": _now()},
                deep=True,
            )
            return event.model_copy(deep=True)

    async def list_run_events(
        self,
        conversation_id: str,
        run_id: str,
        *,
        after: int,
        user_id: str,
        limit: int = 200,
    ) -> tuple[list[RunStreamEvent], Optional[str]]:
        async with self._lock:
            key = (user_id, run_id)
            run = self._runs.get(key)
            if run is None or run.conversation_id != conversation_id:
                return [], None
            events = [
                event.model_copy(deep=True)
                for event in self._run_events.get(key, [])
                if event.id > after
            ][:limit]
            return events, run.status

    async def list_tool_calls(
        self,
        conversation_id: str,
        run_id: str,
        *,
        user_id: str,
        limit: int = 100,
    ) -> list[ToolCallAudit]:
        async with self._lock:
            run = self._runs.get((user_id, run_id))
            if run is None or run.conversation_id != conversation_id:
                return []
            items = sorted(
                (
                    item
                    for (stored_run_id, _), item in self._tool_calls.items()
                    if stored_run_id == run_id
                ),
                key=lambda item: item.sequence_no,
            )[:limit]
            return [item.model_copy(deep=True) for item in items]

    async def get_tool_call(
        self,
        conversation_id: str,
        run_id: str,
        tool_call_id: str,
        *,
        user_id: str,
    ) -> Optional[ToolCallAudit]:
        async with self._lock:
            run = self._runs.get((user_id, run_id))
            if run is None or run.conversation_id != conversation_id:
                return None
            item = self._tool_calls.get((run_id, tool_call_id))
            return item.model_copy(deep=True) if item is not None else None

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
        async with self._lock:
            run = next(
                (
                    item
                    for (_, stored_run_id), item in self._runs.items()
                    if stored_run_id == run_id and item.status == "running"
                ),
                None,
            )
            if run is None:
                raise KeyError(run_id)
            key = (run_id, tool_call_id)
            if key in self._tool_calls:
                raise RuntimeError(f"duplicate tool call: {tool_call_id}")
            item = ToolCallAudit(
                id=uuid4().hex,
                run_id=run_id,
                tool_call_id=tool_call_id,
                round_number=round_number,
                sequence_no=sequence_no,
                tool_name=tool_name,
                status="running",
                arguments=arguments,
                arguments_truncated=arguments_truncated,
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            self._tool_calls[key] = item
            return item.model_copy(deep=True)

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
        now = _now()
        async with self._lock:
            key = (run_id, tool_call_id)
            item = self._tool_calls.get(key)
            if item is None:
                raise KeyError(tool_call_id)
            if item.status != "running":
                raise RuntimeError(f"tool call already finalized: {tool_call_id}")
            updated = item.model_copy(
                update={
                    "status": status,
                    "result_summary": result_summary,
                    "result_size_bytes": result_size_bytes,
                    "result_sha256": result_sha256,
                    "result_truncated": result_truncated,
                    "finished_at": now,
                    "duration_ms": max(0, duration_ms),
                    "error_code": error_code,
                    "error_type": error_type,
                    "error_message": error_message,
                    "failure_stage": failure_stage,
                    "retryable": retryable,
                    "updated_at": now,
                },
                deep=True,
            )
            self._tool_calls[key] = updated
            return updated.model_copy(deep=True)

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
        run_id: Optional[str] = None,
    ) -> None:
        async with self._lock:
            key = (user_id, conversation_id)
            if key not in self._items:
                raise KeyError(conversation_id)
            if run_id is not None:
                latest_run_id = next(
                    (message.run_id for message in reversed(self._items[key].messages)
                     if message.role == "user"),
                    None,
                )
                if latest_run_id != run_id:
                    return
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
        now = _now()
        effective_run_id = run_id or str(uuid4())
        user = ConversationMessage(
            id=uuid4().hex,
            role="user",
            content=question,
            created_at=now,
            updated_at=now,
            run_id=effective_run_id,
        )
        assistant = ConversationMessage(
            id=uuid4().hex,
            role="assistant",
            content=assistant_content,
            created_at=now,
            updated_at=now,
            run_id=effective_run_id,
            status=assistant_status,
            error_code=error_code,
        )
        is_finished = run_status != "running"
        run = ConversationRun(
            id=effective_run_id,
            conversation_id=conversation_id,
            user_message_id=user.id,
            assistant_message_id=assistant.id,
            status=run_status,
            model=model,
            system_prompt_version_id=system_prompt_version_id,
            system_prompt_version_no=system_prompt_version_no,
            system_prompt_sha256=system_prompt_sha256,
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
        async with self._lock:
            item = self._items.get((user_id, conversation_id))
            if item is None:
                raise KeyError(conversation_id)
            item.messages.extend((user, assistant))
            item.message_count = len(item.messages)
            item.updated_at = now
            if item.title == "新会话":
                item.title = question[:30]
            self._runs[(user_id, effective_run_id)] = run
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
        run_status: Optional[str] = None,
        model: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        model_messages: Optional[list[dict]] = None,
        stream_event_sequence: Optional[int] = None,
    ) -> ConversationMessage:
        now = _now()
        async with self._lock:
            for (owner, _), item in self._items.items():
                if owner != user_id:
                    continue
                for index, message in enumerate(item.messages):
                    if message.id == message_id and message.role == "assistant":
                        run = None
                        effective_status = run_status or (
                            "running" if status == "streaming" else status
                        )
                        if message.run_id is not None:
                            run = self._runs.get((user_id, message.run_id))
                            if run is not None and run.status != "running":
                                raise RuntimeError(
                                    f"run {run.id} is not active: {run.status}"
                                )
                        updated = message.model_copy(
                            update={
                                "content": content,
                                "model_messages": model_messages if model_messages is not None else message.model_messages,
                                "sources": sources,
                                "status": status,
                                "stream_event_id": (
                                    stream_event_sequence
                                    if stream_event_sequence is not None
                                    else message.stream_event_id
                                ),
                                "error_code": error_code,
                                "updated_at": now,
                            },
                            deep=True,
                        )
                        item.messages[index] = updated
                        item.updated_at = now
                        if message.run_id is not None:
                            key = (user_id, message.run_id)
                            run = self._runs.get(key)
                            if run is not None:
                                started_at = datetime.fromisoformat(run.started_at)
                                finished_at = datetime.fromisoformat(now)
                                self._runs[key] = run.model_copy(
                                    update={
                                        "status": effective_status,
                                        "model": model or run.model,
                                        "heartbeat_at": now,
                                        "finished_at": (
                                            now if effective_status != "running" else None
                                        ),
                                        "duration_ms": (
                                            max(
                                                0,
                                                int(
                                                    (finished_at - started_at).total_seconds()
                                                    * 1000
                                                ),
                                            )
                                            if effective_status != "running"
                                            else None
                                        ),
                                        "error_code": error_code,
                                        "error_type": error_type,
                                        "error_message": error_message,
                                        "updated_at": now,
                                    },
                                    deep=True,
                                )
                                if effective_status != "running":
                                    self._interrupt_tool_calls(
                                        message.run_id,
                                        now,
                                        error_type=error_type or "RunFinalized",
                                        error_message=(
                                            error_message
                                            or "运行结束时工具调用仍未完成"
                                        ),
                                    )
                        return updated.model_copy(deep=True)
        raise KeyError(message_id)

    async def update_run_token_usage(
        self, run_id: str, usage: dict, *, user_id: str
    ) -> None:
        async with self._lock:
            key = (user_id, run_id)
            run = self._runs.get(key)
            if run is None:
                raise KeyError(run_id)
            self._runs[key] = run.model_copy(
                update={
                    "context_window_tokens": usage.get("context_window_tokens"),
                    "input_budget_tokens": usage.get("input_budget_tokens"),
                    "estimated_input_tokens": usage.get("estimated_input_tokens"),
                    "last_prompt_tokens": usage.get("input_tokens"),
                    "last_completion_tokens": usage.get("output_tokens"),
                    "prompt_tokens": usage.get("cumulative_input_tokens", 0),
                    "completion_tokens": usage.get("cumulative_output_tokens", 0),
                    "total_tokens": usage.get("cumulative_total_tokens", 0),
                    "history_messages_included": usage.get("history_messages_included", 0),
                    "history_messages_dropped": usage.get("history_messages_dropped", 0),
                    "tool_results_truncated": usage.get("tool_results_truncated", 0),
                    "tokenizer_name": usage.get("tokenizer"),
                    "token_usage_source": usage.get("source"),
                    "token_round_number": usage.get("round_number", 0),
                    "updated_at": _now(),
                },
                deep=True,
            )
            conversation = self._items.get((user_id, run.conversation_id))
            if conversation is not None:
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                conversation.context_usage = ContextUsage(
                    **{
                        key: value
                        for key, value in usage.items()
                        if key in ContextUsage.model_fields and key != "used_tokens"
                    },
                    used_tokens=input_tokens + output_tokens,
                )

    async def heartbeat_run(
        self,
        run_id: str,
        *,
        user_id: str,
        owner_instance_id: str,
    ) -> bool:
        del owner_instance_id
        now = _now()
        async with self._lock:
            key = (user_id, run_id)
            run = self._runs.get(key)
            if run is None or run.status != "running":
                return False
            self._runs[key] = run.model_copy(
                update={"heartbeat_at": now, "updated_at": now}, deep=True
            )
            return True

    async def recover_stale_runs(self, stale_before: datetime) -> int:
        now = _now()
        recovered = 0
        async with self._lock:
            for key, run in list(self._runs.items()):
                heartbeat = datetime.fromisoformat(run.heartbeat_at).replace(tzinfo=None)
                if run.status != "running" or heartbeat >= stale_before:
                    continue
                started = datetime.fromisoformat(run.started_at)
                finished = datetime.fromisoformat(now)
                self._runs[key] = run.model_copy(
                    update={
                        "status": "interrupted",
                        "finished_at": now,
                        "duration_ms": max(
                            0, int((finished - started).total_seconds() * 1000)
                        ),
                        "error_code": "AGENT_INTERRUPTED",
                        "error_type": "StaleRunRecovery",
                        "error_message": "服务中断导致运行心跳超时",
                        "updated_at": now,
                    },
                    deep=True,
                )
                self._interrupt_tool_calls(
                    run.id,
                    now,
                    error_type="StaleRunRecovery",
                    error_message="服务中断导致工具调用未完成",
                )
                item = self._items.get((key[0], run.conversation_id))
                if item is not None:
                    for index, message in enumerate(item.messages):
                        if (
                            message.id == run.assistant_message_id
                            and message.status == "streaming"
                        ):
                            item.messages[index] = message.model_copy(
                                update={
                                    "content": message.content
                                    or "服务中断导致回答未完成，请重新发送。",
                                    "status": "failed",
                                    "error_code": "AGENT_INTERRUPTED",
                                    "updated_at": now,
                                },
                                deep=True,
                            )
                    item.updated_at = now
                recovered += 1
        return recovered

    async def recover_cancel_requested_runs(self, stale_before: datetime) -> int:
        recovered = 0
        candidates = []
        async with self._lock:
            for (user_id, run_id), run in self._runs.items():
                if run.status != "cancel_requested" or not run.cancel_requested_at:
                    continue
                requested_at = datetime.fromisoformat(run.cancel_requested_at).replace(tzinfo=None)
                if requested_at < stale_before:
                    candidates.append((user_id, run_id))
        for user_id, run_id in candidates:
            result = await self.finalize_run_cancellation(
                run_id,
                user_id=user_id,
                answer=self._assistant_content(user_id, run_id),
                context_usage={},
                stream_event_sequence=0,
            )
            if result is not None:
                recovered += 1
        return recovered

    def _assistant_content(self, user_id: str, run_id: str) -> str:
        run = self._runs.get((user_id, run_id))
        if run is None:
            return ""
        conversation = self._items.get((user_id, run.conversation_id))
        if conversation is None:
            return ""
        return next(
            (item.content for item in conversation.messages if item.id == run.assistant_message_id),
            "",
        )

    def _interrupt_tool_calls(
        self,
        run_id: str,
        now: str,
        *,
        error_code: str = "AGENT_INTERRUPTED",
        error_type: str,
        error_message: str,
        retryable: bool = True,
    ) -> None:
        finished = datetime.fromisoformat(now)
        for key, item in list(self._tool_calls.items()):
            if item.run_id != run_id or item.status != "running":
                continue
            started = datetime.fromisoformat(item.started_at)
            self._tool_calls[key] = item.model_copy(
                update={
                    "status": "interrupted",
                    "finished_at": now,
                    "duration_ms": max(
                        0, int((finished - started).total_seconds() * 1000)
                    ),
                    "error_code": error_code,
                    "error_type": error_type,
                    "error_message": error_message,
                    "failure_stage": "execution",
                    "retryable": retryable,
                    "updated_at": now,
                },
                deep=True,
            )


conversation_repository = MemoryConversationRepository()
