from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Optional
from uuid import uuid4

from agents.agent_loop import AgentLoop, build_agent_loop
from config.settings import get_settings
from repositories.repository import conversation_repository
from schemas.chat import AgentRequest, ChatMessage
from services.citation_service import CitationService
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
    "observation_service": "观测分析",
    "map_action": "地图操作",
}


class ChatService:
    def __init__(self) -> None:
        self._agent: Optional[AgentLoop] = None
        self._sensitive_word_matcher: Optional[SensitiveWordMatcher] = None
        self._background_tasks: set[asyncio.Task] = set()
        self._maintenance_task: Optional[asyncio.Task] = None
        self._instance_id = str(uuid4())

    async def start(self) -> None:
        if self._maintenance_task is not None:
            return
        await self._recover_stale_runs()
        self._maintenance_task = asyncio.create_task(
            self._recovery_loop(), name="agent-run-recovery"
        )

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
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._agent is not None:
            close = getattr(self._agent, "close", None)
            if close is not None:
                await close()
            self._agent = None

    async def stream_chat(
        self, request: AgentRequest, *, user_id: str
    ) -> AsyncIterator[dict]:
        started_at = perf_counter()
        run_id = str(uuid4())
        contains_sensitive_input = (
            self._sensitive_word_matcher is not None
            and self._sensitive_word_matcher.matches(request.question)
        )
        conversation_id = request.conversation_id
        if not conversation_id:
            conversation = await conversation_repository.create("新会话", user_id=user_id)
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
            yield {
                "event": "conversation",
                "data": {"conversation_id": conversation_id, "run_id": run_id},
            }
            logger.warning(
                "chat_rejected run_id=%s conversation_id=%s reason=sensitive_input",
                run_id,
                conversation_id,
            )
            yield {
                "event": "token",
                "data": {"content": SENSITIVE_INPUT_MESSAGE},
            }
            yield {
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
            }
            return

        # Persist both sides before starting the model. The request/SSE connection is
        # only a subscriber after this point; closing it must not cancel generation.
        _, assistant_message = await conversation_repository.begin_exchange(
            conversation_id,
            request.question,
            run_id=run_id,
            user_id=user_id,
            model=get_settings().llm.model,
            owner_instance_id=self._instance_id,
        )
        event_queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        task = asyncio.create_task(
            self._run_chat(
                request,
                user_id=user_id,
                conversation=conversation,
                conversation_id=conversation_id,
                run_id=run_id,
                assistant_message_id=assistant_message.id,
                started_at=started_at,
                event_queue=event_queue,
            ),
            name=f"agent-chat-{run_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        yield {
            "event": "conversation",
            "data": {"conversation_id": conversation_id, "run_id": run_id},
        }
        while True:
            event = await event_queue.get()
            if event is None:
                return
            yield event

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
        event_queue: asyncio.Queue,
    ) -> None:
        answer_parts: list[str] = []
        registered_sources: list[dict] = []
        retrieval_counts: dict[str, int] = {}
        sequence = 0
        last_persisted_length = 0
        last_persisted_at = perf_counter()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(run_id, user_id),
            name=f"agent-heartbeat-{run_id}",
        )
        try:
            active_context = request.context
            if (
                active_context is not None
                and active_context.drawn_geometry is not None
            ):
                await conversation_repository.save_map_context(
                    conversation_id,
                    active_context,
                    user_id=user_id,
                )
            else:
                active_context = await conversation_repository.get_map_context(
                    conversation_id, user_id=user_id
                )
            # The authenticated conversation is the sole source of history.
            # Ignore client history (including stale history after account switches).
            history = [
                ChatMessage(role=message.role, content=message.content)
                for message in conversation.messages
            ]
            logger.info(
                "chat_started run_id=%s conversation_id=%s question=%r "
                "history=%r",
                run_id,
                conversation_id,
                request.question,
                history,
            )
            run_stream_parameters = inspect.signature(
                self.agent.run_stream
            ).parameters
            run_stream_kwargs = {}
            if "run_id" in run_stream_parameters:
                run_stream_kwargs["run_id"] = run_id
            if "frontend_context" in run_stream_parameters:
                run_stream_kwargs["frontend_context"] = active_context
            if "audit_recorder" in run_stream_parameters:
                run_stream_kwargs["audit_recorder"] = PersistentToolAuditRecorder(
                    run_id,
                    repository=conversation_repository,
                )
            async for event in self.agent.run_stream(
                request.question,
                history,
                **run_stream_kwargs,
            ):
                sequence += 1
                if event.type == "tool_started":
                    if get_settings().stream.include_process:
                        tool_display_name = self._tool_display_name(event.tool_name)
                        await event_queue.put({
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
                    if event.sources is not None:
                        registered_sources = event.sources
                    result_count = data.get("result_count")
                    if isinstance(result_count, int) and not isinstance(
                        result_count, bool
                    ):
                        retrieval_counts[event.tool_call_id] = result_count
                    await event_queue.put({"event": "retrieval", "data": data})
                elif event.type == "map_action":
                    data = event.data or {}
                    action_sequence = int(data.get("seq") or 1)
                    await event_queue.put({
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
                        await event_queue.put({
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
                    answer_parts.append(event.content)
                    await event_queue.put(
                        {"event": "token", "data": {"content": event.content}}
                    )
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
                        )
                        last_persisted_length = len(draft)
                        last_persisted_at = now
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
                "answer=%r sources=%r",
                run_id,
                conversation_id,
                (perf_counter() - started_at) * 1000,
                answer,
                cited_sources,
            )
            await event_queue.put({
                "event": "done",
                "data": {
                    "conversation_id": conversation_id,
                    "run_id": run_id,
                    "answer": answer,
                    "model": self.agent.model_name,
                    "sources": cited_sources,
                    "images": [],
                    "assistant_message_id": message.id,
                },
            })
        except asyncio.CancelledError:
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
            await event_queue.put({
                "event": "error",
                "data": {
                    "message": "对话生成失败，请稍后重试",
                    "error_code": "AGENT_STREAM_FAILED",
                    "retryable": True,
                },
            })
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await event_queue.put(None)

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

    async def _recover_stale_runs(self) -> None:
        settings = get_settings().conversation
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            seconds=settings.run_lease_seconds
        )
        recovered = await conversation_repository.recover_stale_runs(cutoff)
        if recovered:
            logger.warning("stale_agent_runs_recovered count=%d", recovered)

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


chat_service = ChatService()
