from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Optional

from agents.agent_loop import AgentLoop, build_agent_loop
from config.settings import get_settings
from repositories.memory_conversation_repository import conversation_repository
from schemas.chat import AgentRequest


logger = logging.getLogger(__name__)


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
        conversation_id = request.conversation_id
        if not conversation_id:
            conversation_id = (await conversation_repository.create("新会话")).id
        elif await conversation_repository.get(conversation_id) is None:
            yield {
                "event": "error",
                "data": {"message": "会话不存在", "error_code": "CONVERSATION_NOT_FOUND", "retryable": False},
            }
            return
        yield {"event": "conversation", "data": {"conversation_id": conversation_id}}
        answer_parts: list[str] = []
        sequence = 0
        try:
            async for event in self.agent.run_stream(
                request.question, request.chat_history
            ):
                sequence += 1
                if event.type == "tool_started":
                    if get_settings().stream.include_process:
                        yield {
                            "event": "process",
                            "data": self._process_data(
                                event.round_number,
                                "tool",
                                "tool",
                                "running",
                                f"调用工具：{event.tool_name}",
                                "工具正在执行",
                                sequence,
                                step_id=event.tool_call_id,
                            ),
                        }
                elif event.type == "tool_finished":
                    if get_settings().stream.include_process:
                        summary = (
                            event.content
                            if get_settings().stream.include_tool_result
                            else f"{event.tool_name or '工具'}调用{'完成' if event.success else '失败'}"
                        )
                        yield {
                            "event": "process",
                            "data": self._process_data(
                                event.round_number,
                                "tool",
                                "tool",
                                "success" if event.success else "failed",
                                f"调用工具：{event.tool_name}",
                                summary,
                                sequence,
                                step_id=event.tool_call_id,
                                recoverable=True,
                            ),
                        }
                elif event.type == "answer_delta":
                    answer_parts.append(event.content)
                    yield {"event": "token", "data": {"content": event.content}}
            answer = "".join(answer_parts)
            if not answer.strip():
                raise RuntimeError("AgentLoop 未生成有效回答")
            message = await conversation_repository.save_exchange(
                conversation_id, request.question, answer
            )
            yield {
                "event": "done",
                "data": {
                    "conversation_id": conversation_id,
                    "answer": answer,
                    "model": self.agent.model_name,
                    "sources": [],
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
        }


chat_service = ChatService()
