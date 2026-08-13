from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Optional

from agents.react_agent import CoreAgent, build_react_agent
from config.settings import get_settings
from repositories.memory_conversation_repository import conversation_repository
from schemas.chat import AgentRequest


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self) -> None:
        self._agent: Optional[CoreAgent] = None

    @property
    def agent(self) -> CoreAgent:
        if self._agent is None:
            self._agent = build_react_agent()
        return self._agent

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
        think_buffers: dict[int, str] = {}
        sequence = 0
        try:
            async for event in self.agent.run_stream(
                request.question, request.chat_history
            ):
                sequence += 1
                if event.type == "think":
                    if not get_settings().stream.include_process:
                        continue
                    summary = think_buffers.get(event.round_number, "") + event.content
                    think_buffers[event.round_number] = summary
                    yield {
                        "event": "process",
                        "data": self._process_data(
                            event.round_number,
                            "reasoning",
                            "reasoning",
                            "running",
                            f"第 {event.round_number} 轮分析",
                            summary,
                            sequence,
                        ),
                    }
                elif event.type == "think_finished":
                    if get_settings().stream.include_process:
                        yield {
                            "event": "process",
                            "data": self._process_data(
                                event.round_number,
                                "reasoning",
                                "reasoning",
                                "success",
                                f"第 {event.round_number} 轮分析",
                                event.content or "分析完成",
                                sequence,
                            ),
                        }
                elif event.type == "answer":
                    answer_parts.append(event.content)
                    yield {"event": "token", "data": {"content": event.content}}
            answer = "".join(answer_parts)
            if not answer.strip():
                raise RuntimeError("ReAct 未生成有效回答")
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
            logger.exception("ReAct chat stream failed")
            yield {
                "event": "error",
                "data": {
                    "message": "对话生成失败，请稍后重试",
                    "error_code": "REACT_STREAM_FAILED",
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
        recoverable: bool = True,
    ) -> dict:
        return {
            "agent_id": "react-agent",
            "step_id": f"{kind}-{round_number}",
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
