from __future__ import annotations

import asyncio
import inspect
import logging
import re
from hashlib import sha256
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Optional
from uuid import uuid4

from agents.agent_loop import AgentLoop, build_agent_loop
from config.settings import get_settings
from repositories.repository import conversation_repository
from schemas.system_prompt import ActiveSystemPromptSnapshot
from schemas.skill import ActiveSkillSnapshot
from schemas.chat import AgentRequest, ChatMessage, FrontendContext
from services.citation_service import CitationService
from services.context_service import SYSTEM_PROMPT
from services.emoji_filter import EmojiStreamFilter
from services.recommendation_service import generate_recommendations
from services.sensitive_word_service import SensitiveWordMatcher
from services.tool_audit_service import PersistentToolAuditRecorder


logger = logging.getLogger(__name__)

SENSITIVE_INPUT_MESSAGE = "您的输入包含敏感词，请修改后重新提问。"
_SECRET_PATTERN = re.compile(
    r"(?i)(password|token|api[_-]?key)\s*[=:]\s*([^\s,;]+)"
)
_URL_PASSWORD_PATTERN = re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)")

_TOOL_DISPLAY_NAMES = {
    "load_skill": "能力加载",
    "CurrentServerTime": "当前服务器时间",
    "rag_search": "东溟知识库检索",
    "database_query": "数据库查询",
    "spatial_query_service": "通用空间查询服务",
    "map_action": "地图操作",
    "dm_duanmian_analysis": "断面专题分析",
    "analyze_suspicion": "疑点疑区研判",
    "query_suspicion_results": "疑点疑区结果查询",
    "summarize_suspicion_results": "疑点疑区结果统计",
}


class _LiveEventSink:
    """Best-effort delivery to the initial HTTP subscriber.

    Durable events are the source of truth. Closing or overflowing this queue only
    ends the live connection; it must never stop the background generation task.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Optional[dict]] = asyncio.Queue(maxsize=512)
        self.closed = False

    def publish(self, event: dict) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.close()

    def finish(self) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass


class ChatService:
    def __init__(self, prompt_provider=None, skill_provider=None) -> None:
        self._agent: Optional[AgentLoop] = None
        self._sensitive_word_matcher: Optional[SensitiveWordMatcher] = None
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._maintenance_task: Optional[asyncio.Task] = None
        self._instance_id = str(uuid4())
        self._prompt_provider = prompt_provider or _StaticPromptProvider()
        self._skill_provider = skill_provider

    async def start(self) -> None:
        if self._maintenance_task is not None:
            return
        await self._recover_stale_runs()
        self._maintenance_task = asyncio.create_task(
            self._recovery_loop(), name="agent-run-recovery"
        )

    def set_prompt_provider(self, prompt_provider) -> None:
        self._prompt_provider = prompt_provider

    def set_skill_provider(self, skill_provider) -> None:
        self._skill_provider = skill_provider

    def replace_sensitive_word_matcher(self, rules: list[dict]) -> None:
        matcher = SensitiveWordMatcher(rules)
        self._sensitive_word_matcher = matcher

    @property
    def agent(self) -> AgentLoop:
        if self._agent is None:
            self._agent = build_agent_loop()
        return self._agent

    async def close(self) -> None:
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None
        tasks = list(self._background_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._agent is not None:
            close = getattr(self._agent, "close", None)
            if close is not None:
                await close()
            self._agent = None

    async def cancel_run(
        self, conversation_id: str, run_id: str, *, user_id: str
    ):
        run = await conversation_repository.request_run_cancellation(
            conversation_id, run_id, user_id=user_id
        )
        if run is None:
            return None
        if run.status == "cancel_requested":
            task = self._background_tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel("USER_CANCELLED")
        return run

    async def stream_chat(
        self, request: AgentRequest, *, user_id: str, identity=None
    ) -> AsyncIterator[dict]:
        started_at = perf_counter()
        run_id = str(uuid4())
        contains_sensitive_input = (
            self._sensitive_word_matcher is not None
            and self._sensitive_word_matcher.matches(request.question)
        )
        conversation_id = request.conversation_id
        if not conversation_id:
            conversation = await conversation_repository.create("新会话", user_id=user_id, identity=identity)
            conversation_id = conversation.id
        else:
            conversation = await conversation_repository.get(conversation_id, user_id=user_id)
            if conversation is None:
                if contains_sensitive_input:
                    logger.warning(
                        "chat_rejected run_id=%s conversation_id=%s "
                        "reason=conversation_not_found sensitive_input=true",
                        run_id,
                        conversation_id,
                    )
                else:
                    logger.warning(
                        "chat_rejected run_id=%s conversation_id=%s question=%r "
                        "reason=conversation_not_found",
                        run_id,
                        conversation_id,
                        request.question,
                    )
                yield {
                    "event": "error",
                    "data": {
                        "message": "会话不存在",
                        "error_code": "CONVERSATION_NOT_FOUND",
                        "retryable": False,
                    },
                }
                return
        if contains_sensitive_input:
            _, assistant_message = await conversation_repository.begin_exchange(
                conversation_id,
                request.question,
                run_id=run_id,
                user_id=user_id,
                model="sensitive-word-filter",
                owner_instance_id=self._instance_id,
                assistant_content=SENSITIVE_INPUT_MESSAGE,
                assistant_status="failed",
                run_status="rejected",
                error_code="SENSITIVE_INPUT_REJECTED",
                error_type="SensitiveInputRejected",
                error_message="用户输入命中敏感词规则",
            )
            conversation_event = await self._persist_event(run_id, user_id, {
                "event": "conversation",
                "data": {"conversation_id": conversation_id, "run_id": run_id},
            })
            yield conversation_event
            logger.warning(
                "chat_rejected run_id=%s conversation_id=%s reason=sensitive_input",
                run_id,
                conversation_id,
            )
            token_event = await self._persist_event(run_id, user_id, {
                "event": "token",
                "data": {"content": SENSITIVE_INPUT_MESSAGE},
            })
            yield token_event
            done_event = await self._persist_event(run_id, user_id, {
                "event": "done",
                "data": {
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "answer": SENSITIVE_INPUT_MESSAGE,
                    "model": "sensitive-word-filter",
                    "sources": [],
                    "images": [],
                    "assistant_message_id": assistant_message.id,
                },
            })
            yield done_event
            return

        prompt_snapshot = await self._prompt_provider.active_snapshot()
        # 应用启动时注入数据库服务；读取失败直接终止，不回退为全部启用。
        skill_snapshot = (
            await self._skill_provider.active_snapshot()
            if self._skill_provider is not None else None
        )
        # Persist both sides before starting the model. The request/SSE connection is
        # only a subscriber after this point; closing it must not cancel generation.
        _, assistant_message = await conversation_repository.begin_exchange(
            conversation_id,
            request.question,
            run_id=run_id,
            user_id=user_id,
            model=get_settings().llm.model,
            system_prompt_version_id=prompt_snapshot.version_id,
            system_prompt_version_no=prompt_snapshot.version_no,
            system_prompt_sha256=prompt_snapshot.content_sha256,
            owner_instance_id=self._instance_id,
        )
        event_sink = _LiveEventSink()
        conversation_event = await self._persist_event(run_id, user_id, {
            "event": "conversation",
            "data": {"conversation_id": conversation_id, "run_id": run_id},
        })
        task = asyncio.create_task(
            self._run_chat(
                request,
                user_id=user_id,
                conversation=conversation,
                conversation_id=conversation_id,
                run_id=run_id,
                assistant_message_id=assistant_message.id,
                started_at=started_at,
                event_sink=event_sink,
                prompt_snapshot=prompt_snapshot,
                skill_snapshot=skill_snapshot,
            ),
            name=f"agent-chat-{run_id}",
        )
        self._background_tasks[run_id] = task
        task.add_done_callback(
            lambda completed, active_run_id=run_id: self._discard_background_task(
                active_run_id, completed
            )
        )
        yield conversation_event
        try:
            while True:
                event = await event_sink.queue.get()
                if event is None:
                    return
                yield event
        finally:
            event_sink.close()

    async def _run_chat(
        self,
        request: AgentRequest,
        *,
        user_id: str,
        conversation,
        conversation_id: str,
        run_id: str,
        assistant_message_id: str,
        started_at: float,
        event_sink: _LiveEventSink,
        prompt_snapshot: ActiveSystemPromptSnapshot,
        skill_snapshot: Optional[ActiveSkillSnapshot],
    ) -> None:
        answer_parts: list[str] = []
        emoji_filter = EmojiStreamFilter()
        registered_sources: list[dict] = []
        retrieval_counts: dict[str, int] = {}
        latest_context_usage: dict = {}
        sequence = 0
        latest_token_event_sequence = 0
        last_persisted_length = 0
        last_persisted_at = perf_counter()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(run_id, user_id),
            name=f"agent-heartbeat-{run_id}",
        )
        cancellation_task = asyncio.create_task(
            self._cancellation_loop(
                conversation_id, run_id, user_id, asyncio.current_task()
            ),
            name=f"agent-cancellation-{run_id}",
        )

        async def emit(event: dict) -> int:
            stored = await self._persist_event(run_id, user_id, event)
            event_sink.publish(stored)
            return int(stored["id"])

        try:
            # The authenticated conversation is the sole source of history.
            # Ignore client history (including stale history after account switches).
            history = []
            for message in conversation.messages:
                if message.role == "assistant" and message.model_messages:
                    history.extend(
                        ChatMessage.model_validate(item) for item in message.model_messages
                    )
                    if message.status == "failed":
                        history.append(ChatMessage(role="assistant", content=message.content))
                elif message.content:
                    history.append(ChatMessage(role=message.role, content=message.content))
            if "context" in request.model_fields_set:
                active_context = request.context or FrontendContext()
                await conversation_repository.save_map_context(
                    conversation_id, active_context, user_id=user_id, run_id=run_id,
                )
            else:
                active_context = await conversation_repository.get_map_context(
                    conversation_id, user_id=user_id,
                )
            logger.info(
                "chat_started run_id=%s conversation_id=%s question=%r "
                "history_count=%d",
                run_id,
                conversation_id,
                request.question[:1000],
                len(history),
            )
            run_stream_parameters = inspect.signature(
                self.agent.run_stream
            ).parameters
            run_stream_kwargs = {}

            async def save_model_messages(messages: list[dict]) -> None:
                await conversation_repository.update_assistant_message(
                    assistant_message_id,
                    "".join(answer_parts),
                    [],
                    status="streaming",
                    user_id=user_id,
                    model_messages=messages,
                    stream_event_sequence=latest_token_event_sequence,
                )

            async def publish_context_usage(usage: dict) -> None:
                nonlocal latest_context_usage
                latest_context_usage = dict(usage)
                await conversation_repository.update_run_token_usage(
                    run_id, latest_context_usage, user_id=user_id
                )
                await emit({"event": "context_usage", "data": latest_context_usage})

            if "run_id" in run_stream_parameters:
                run_stream_kwargs["run_id"] = run_id
            if "frontend_context" in run_stream_parameters:
                run_stream_kwargs["frontend_context"] = active_context
            if "audit_recorder" in run_stream_parameters:
                run_stream_kwargs["audit_recorder"] = PersistentToolAuditRecorder(
                    run_id,
                    repository=conversation_repository,
                )
            if "system_prompt" in run_stream_parameters:
                run_stream_kwargs["system_prompt"] = prompt_snapshot.content
            if skill_snapshot is not None and "enabled_skill_names" in run_stream_parameters:
                run_stream_kwargs["enabled_skill_names"] = skill_snapshot.enabled_skill_names
            if "context_enabled" in run_stream_parameters:
                run_stream_kwargs["context_enabled"] = conversation.context_enabled
            if "context_message_limit" in run_stream_parameters:
                run_stream_kwargs["context_message_limit"] = conversation.context_message_limit
            if "context_token_limit" in run_stream_parameters:
                run_stream_kwargs["context_token_limit"] = conversation.context_token_limit
            if "context_usage_callback" in run_stream_parameters:
                run_stream_kwargs["context_usage_callback"] = publish_context_usage
            if "model_messages_callback" in run_stream_parameters:
                run_stream_kwargs["model_messages_callback"] = save_model_messages
            async def publish_tool_progress(data):
                nonlocal sequence
                if not get_settings().stream.include_process:
                    return
                sequence += 1
                process = self._process_data(
                    data["round_number"], "tool", data.get("kind", "tool"), data["status"],
                    data["title"], data["summary"], sequence,
                    step_id=data["step_id"], details=data.get("details", {}),
                )
                process["parent_id"] = data["parent_id"]
                await emit({"event": "process", "data": process})

            if "tool_progress_callback" in run_stream_parameters:
                run_stream_kwargs["tool_progress_callback"] = publish_tool_progress
            async for event in self.agent.run_stream(
                request.question,
                history,
                **run_stream_kwargs,
            ):
                sequence += 1
                if event.sources is not None:
                    registered_sources = event.sources
                if event.type == "tool_started":
                    if get_settings().stream.include_process:
                        tool_display_name = self._tool_display_name(event.tool_name)
                        await emit({
                            "event": "process",
                            "data": self._process_data(
                                event.round_number,
                                "tool",
                                "tool",
                                "running",
                                f"算法工具箱：{tool_display_name}",
                                f"{tool_display_name}正在执行",
                                sequence,
                                step_id=event.tool_call_id,
                            ),
                        })
                elif event.type == "retrieval":
                    data = {
                        **(event.data or {}),
                        "tool_name": self._tool_display_name(event.tool_name),
                    }
                    result_count = data.get("result_count")
                    if isinstance(result_count, int) and not isinstance(
                        result_count, bool
                    ):
                        retrieval_counts[event.tool_call_id] = result_count
                    await emit({"event": "retrieval", "data": data})
                elif event.type == "map_action":
                    data = event.data or {}
                    if (
                        event.tool_name == "map_action"
                        and data.get("action_type") == "CLEAR"
                        and data.get("clear_selection") is True
                    ):
                        await conversation_repository.save_map_context(
                            conversation_id, FrontendContext(), user_id=user_id, run_id=run_id,
                        )
                    action_sequence = int(data.get("seq") or 1)
                    await emit({
                        "event": "map_action",
                        "data": {
                            "version": "1.0",
                            "conversation_id": conversation_id,
                            "run_id": run_id,
                            "batch_id": event.tool_call_id,
                            "action_id": f"{event.tool_call_id}-{action_sequence}",
                            "seq": action_sequence,
                            "action_type": data.get("action_type"),
                            "payload": data.get("payload") or {},
                        },
                    })
                elif event.type == "tool_finished":
                    if get_settings().stream.include_process:
                        tool_display_name = self._tool_display_name(event.tool_name)
                        summary = (
                            event.content
                            if not event.success
                            or get_settings().stream.include_tool_result
                            else f"{tool_display_name}执行{'完成' if event.success else '失败'}"
                        )
                        await emit({
                            "event": "process",
                            "data": self._process_data(
                                event.round_number,
                                "tool",
                                "tool",
                                "success" if event.success else "failed",
                                f"算法工具箱：{tool_display_name}",
                                summary,
                                sequence,
                                step_id=event.tool_call_id,
                                recoverable=True,
                                details=(
                                    {"result_count": retrieval_counts[event.tool_call_id]}
                                    if event.success
                                    and event.tool_call_id in retrieval_counts
                                    else {}
                                ),
                            ),
                        })
                elif event.type == "answer_delta":
                    content = emoji_filter.feed(event.content)
                    if not content:
                        continue
                    answer_parts.append(content)
                    event_sequence = await emit(
                        {"event": "token", "data": {"content": content}}
                    )
                    latest_token_event_sequence = event_sequence
                    draft = "".join(answer_parts)
                    now = perf_counter()
                    if (
                        len(draft) - last_persisted_length >= 80
                        or now - last_persisted_at >= 0.75
                    ):
                        await conversation_repository.update_assistant_message(
                            assistant_message_id,
                            draft,
                            [],
                            status="streaming",
                            user_id=user_id,
                            stream_event_sequence=event_sequence,
                        )
                        last_persisted_length = len(draft)
                        last_persisted_at = now
            tail = emoji_filter.feed("", final=True)
            if tail:
                answer_parts.append(tail)
                latest_token_event_sequence = await emit(
                    {"event": "token", "data": {"content": tail}}
                )
            answer = "".join(answer_parts)
            if not answer.strip():
                raise RuntimeError("AgentLoop 未生成有效回答")
            answer, cited_sources = CitationService.finalize(
                answer, registered_sources
            )
            message = await conversation_repository.update_assistant_message(
                assistant_message_id,
                answer,
                cited_sources,
                status="completed",
                user_id=user_id,
                run_status="completed",
                model=self.agent.model_name,
            )
            logger.info(
                "chat_completed run_id=%s conversation_id=%s duration_ms=%.1f "
                "answer_chars=%d source_count=%d",
                run_id,
                conversation_id,
                (perf_counter() - started_at) * 1000,
                len(answer),
                len(cited_sources),
            )
            await emit({
                "event": "done",
                "data": {
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "answer": answer,
                    "model": self.agent.model_name,
                    "sources": cited_sources,
                    "images": [],
                    "assistant_message_id": message.id,
                    "context_usage": latest_context_usage,
                },
            })
        except asyncio.CancelledError as exc:
            run = await conversation_repository.get_run(
                conversation_id, run_id, user_id=user_id
            )
            user_cancelled = (
                "USER_CANCELLED" in exc.args
                or (run is not None and run.status == "cancel_requested")
            )
            if user_cancelled:
                tail = emoji_filter.feed("", final=True)
                if tail:
                    answer_parts.append(tail)
                await self._finalize_user_cancellation(
                    run_id=run_id,
                    user_id=user_id,
                    answer="".join(answer_parts),
                    context_usage=latest_context_usage,
                    stream_event_sequence=latest_token_event_sequence,
                    event_sink=event_sink,
                )
                logger.info(
                    "chat_cancelled run_id=%s conversation_id=%s duration_ms=%.1f",
                    run_id,
                    conversation_id,
                    (perf_counter() - started_at) * 1000,
                )
            else:
                await conversation_repository.update_assistant_message(
                    assistant_message_id,
                    "".join(answer_parts) or "服务重启导致回答中断，请重新发送。",
                    [],
                    status="failed",
                    error_code="AGENT_INTERRUPTED",
                    user_id=user_id,
                    run_status="interrupted",
                    model=self._resolved_model_name(),
                    error_type="CancelledError",
                    error_message="服务关闭导致回答任务被取消",
                )
                raise
        except Exception as exc:
            run = await conversation_repository.get_run(
                conversation_id, run_id, user_id=user_id
            )
            if run is not None and run.status == "cancel_requested":
                await self._finalize_user_cancellation(
                    run_id=run_id,
                    user_id=user_id,
                    answer="".join(answer_parts),
                    context_usage=latest_context_usage,
                    stream_event_sequence=latest_token_event_sequence,
                    event_sink=event_sink,
                )
                return
            logger.exception(
                "chat_failed run_id=%s conversation_id=%s duration_ms=%.1f "
                "question=%r exception_type=%s",
                run_id,
                conversation_id,
                (perf_counter() - started_at) * 1000,
                request.question,
                type(exc).__name__,
            )
            await conversation_repository.update_assistant_message(
                assistant_message_id,
                "".join(answer_parts) or "对话生成失败，请重新发送。",
                [],
                status="failed",
                error_code="AGENT_STREAM_FAILED",
                user_id=user_id,
                run_status="failed",
                model=self._resolved_model_name(),
                error_type=type(exc).__name__,
                error_message=self._safe_error_message(exc),
            )
            await emit({
                "event": "error",
                "data": {
                    "message": "对话生成失败，请稍后重试",
                    "error_code": "AGENT_STREAM_FAILED",
                    "retryable": True,
                },
            })
        else:
            if not event_sink.closed:
                questions = []
                try:
                    questions = await asyncio.wait_for(
                        generate_recommendations(
                            self.agent, conversation, request.question, answer, message.id
                        ),
                        timeout=20,
                    )
                except Exception:
                    logger.exception("recommendations_failed run_id=%s", run_id)
                # Transient suggestions: do not write them to the message or event log.
                event_sink.publish({
                    "event": "recommendations",
                    "data": {
                        "conversation_id": conversation_id,
                        "message_id": message.id,
                        "questions": questions,
                    },
                })
        finally:
            heartbeat_task.cancel()
            cancellation_task.cancel()
            await asyncio.gather(
                heartbeat_task, cancellation_task, return_exceptions=True
            )
            event_sink.finish()

    async def _finalize_user_cancellation(
        self,
        *,
        run_id: str,
        user_id: str,
        answer: str,
        context_usage: dict,
        stream_event_sequence: int,
        event_sink: _LiveEventSink,
    ) -> None:
        result = await conversation_repository.finalize_run_cancellation(
            run_id,
            user_id=user_id,
            answer=answer,
            context_usage=context_usage,
            stream_event_sequence=stream_event_sequence,
        )
        if result is None:
            return
        _, stored = result
        event_sink.publish({
            "id": stored.id,
            "event": stored.event,
            "data": stored.data,
        })

    async def _persist_event(self, run_id: str, user_id: str, event: dict) -> dict:
        stored = await conversation_repository.append_run_event(
            run_id,
            str(event["event"]),
            dict(event.get("data") or {}),
            user_id=user_id,
        )
        return {"id": stored.id, "event": stored.event, "data": stored.data}

    async def _heartbeat_loop(self, run_id: str, user_id: str) -> None:
        interval = get_settings().conversation.run_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            active = await conversation_repository.heartbeat_run(
                run_id,
                user_id=user_id,
                owner_instance_id=self._instance_id,
            )
            if not active:
                return

    async def _cancellation_loop(
        self,
        conversation_id: str,
        run_id: str,
        user_id: str,
        target_task: Optional[asyncio.Task],
    ) -> None:
        interval = get_settings().conversation.run_cancel_poll_seconds
        while True:
            await asyncio.sleep(interval)
            run = await conversation_repository.get_run(
                conversation_id, run_id, user_id=user_id
            )
            if run is None or run.status not in {"running", "cancel_requested"}:
                return
            if run.status == "cancel_requested":
                if target_task is not None and not target_task.done():
                    target_task.cancel("USER_CANCELLED")
                return

    async def _recover_stale_runs(self) -> None:
        settings = get_settings().conversation
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=settings.run_lease_seconds
        )
        recovered = await conversation_repository.recover_stale_runs(cutoff)
        if recovered:
            logger.warning("stale_agent_runs_recovered count=%d", recovered)
        cancel_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=settings.run_cancel_timeout_seconds
        )
        cancelled = await conversation_repository.recover_cancel_requested_runs(
            cancel_cutoff
        )
        if cancelled:
            logger.warning("cancel_requested_runs_recovered count=%d", cancelled)

    async def _recovery_loop(self) -> None:
        interval = get_settings().conversation.run_recovery_interval_seconds
        while True:
            await asyncio.sleep(interval)
            try:
                await self._recover_stale_runs()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("stale_agent_run_recovery_failed")

    def _resolved_model_name(self) -> str:
        return self._agent.model_name if self._agent is not None else get_settings().llm.model

    def _discard_background_task(
        self, run_id: str, task: asyncio.Task
    ) -> None:
        if self._background_tasks.get(run_id) is task:
            self._background_tasks.pop(run_id, None)

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        message = str(exc).strip() or type(exc).__name__
        message = _SECRET_PATTERN.sub(r"\1=***", message)
        message = _URL_PASSWORD_PATTERN.sub(r"\1***\2", message)
        return message[:2000]

    @staticmethod
    def _tool_display_name(tool_name: str) -> str:
        return _TOOL_DISPLAY_NAMES.get(tool_name, "通用算法")

    @staticmethod
    def _process_data(
        round_number: int,
        stage: str,
        kind: str,
        status: str,
        title: str,
        summary: str,
        sequence: int,
        *,
        step_id: str = "",
        recoverable: bool = True,
        details: Optional[dict] = None,
    ) -> dict:
        return {
            "agent_id": "agent-loop",
            "step_id": step_id or f"{kind}-{round_number}",
            "parent_id": None,
            "stage": stage,
            "kind": kind,
            "status": status,
            "severity": "warning" if status == "failed" else "info",
            "title": title,
            "summary": summary,
            "sequence": sequence,
            "recoverable": recoverable,
            "retryable": False,
            "details": details or {},
        }


class _StaticPromptProvider:
    """Keep isolated ChatService unit tests independent from application startup."""

    async def active_snapshot(self) -> ActiveSystemPromptSnapshot:
        return ActiveSystemPromptSnapshot(
            version_id=0,
            version_no=0,
            content=SYSTEM_PROMPT,
            content_sha256=sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        )


chat_service = ChatService()
