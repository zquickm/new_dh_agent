from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Optional
from uuid import uuid4

from agents.agent_loop import AgentLoop, build_agent_loop
from config.settings import get_settings
from repositories.memory_conversation_repository import conversation_repository
from schemas.chat import AgentRequest
from services.citation_service import CitationService
from services.request_context_adapter import enrich_question_with_frontend_context


logger = logging.getLogger(__name__)

_TOOL_DISPLAY_NAMES = {
    "CurrentServerTime": "当前服务器时间",
    "rag_search": "知识库检索",
    "database_query": "数据库查询",
    "observation_service": "观测分析",
    "map_action": "地图操作",
}


class ChatService:
    def __init__(self) -> None:
        self._agent: Optional[AgentLoop] = None

    @property
    def agent(self) -> AgentLoop:
        if self._agent is None:
            self._agent = build_agent_loop()
        return self._agent

    async def close(self) -> None:
        if self._agent is not None:
            close = getattr(self._agent, "close", None)
            if close is not None:
                await close()
            self._agent = None

    async def stream_chat(self, request: AgentRequest) -> AsyncIterator[dict]:
        run_id = str(uuid4())
        conversation_id = request.conversation_id
        if not conversation_id:
            conversation_id = (await conversation_repository.create("新会话")).id
        elif await conversation_repository.get(conversation_id) is None:
            yield {
                "event": "error",
                "data": {"message": "会话不存在", "error_code": "CONVERSATION_NOT_FOUND", "retryable": False},
            }
            return
        yield {
            "event": "conversation",
            "data": {"conversation_id": conversation_id, "run_id": run_id},
        }
        answer_parts: list[str] = []
        registered_sources: list[dict] = []
        retrieval_counts: dict[str, int] = {}
        sequence = 0
        try:
            active_context = request.context
            if (
                active_context is not None
                and active_context.drawn_geometry is not None
            ):
                await conversation_repository.save_map_context(
                    conversation_id,
                    active_context,
                )
            else:
                active_context = await conversation_repository.get_map_context(
                    conversation_id
                )
            agent_question = enrich_question_with_frontend_context(
                request.question,
                active_context,
            )
            async for event in self.agent.run_stream(
                agent_question, request.chat_history
            ):
                sequence += 1
                if event.type == "tool_started":
                    if get_settings().stream.include_process:
                        tool_display_name = self._tool_display_name(event.tool_name)
                        yield {
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
                        }
                elif event.type == "retrieval":
                    data = event.data or {}
                    if event.sources is not None:
                        registered_sources = event.sources
                    result_count = data.get("result_count")
                    if isinstance(result_count, int) and not isinstance(
                        result_count, bool
                    ):
                        retrieval_counts[event.tool_call_id] = result_count
                    yield {"event": "retrieval", "data": data}
                elif event.type == "map_action":
                    data = event.data or {}
                    action_sequence = int(data.get("seq") or 1)
                    yield {
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
                    }
                elif event.type == "tool_finished":
                    if get_settings().stream.include_process:
                        tool_display_name = self._tool_display_name(event.tool_name)
                        summary = (
                            event.content
                            if get_settings().stream.include_tool_result
                            else f"{tool_display_name}执行{'完成' if event.success else '失败'}"
                        )
                        yield {
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
                        }
                elif event.type == "answer_delta":
                    answer_parts.append(event.content)
                    yield {"event": "token", "data": {"content": event.content}}
            answer = "".join(answer_parts)
            if not answer.strip():
                raise RuntimeError("AgentLoop 未生成有效回答")
            answer, cited_sources = CitationService.finalize(
                answer, registered_sources
            )
            message = await conversation_repository.save_exchange(
                conversation_id, request.question, answer, cited_sources
            )
            yield {
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
            }
        except Exception:
            logger.exception("Agent chat stream failed")
            yield {
                "event": "error",
                "data": {
                    "message": "对话生成失败，请稍后重试",
                    "error_code": "AGENT_STREAM_FAILED",
                    "retryable": True,
                },
            }

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
