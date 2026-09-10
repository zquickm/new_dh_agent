from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Any, Optional

from agents.citations import CitationRegistry
from agents.events import AgentEvent
from agents.llm_client import HelloAgentsLLM
from agents.skills import SkillCatalog
from agents.tool_audit import ToolAuditRecorder
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_skill_catalog, build_tool_executor
from config.settings import get_settings
from schemas.chat import ChatMessage, FrontendContext
from services.context_service import ContextService


logger = logging.getLogger(__name__)


def _classify_tool_error(
    exc: Exception,
    *,
    tool_name: str,
    failure_stage: str,
) -> tuple[str, str, str, bool]:
    error_type = type(exc).__name__
    detail = str(exc).strip() or error_type
    lowered_type = error_type.lower()
    if failure_stage == "result_processing":
        return "TOOL_RESULT_INVALID", error_type, detail, False
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "TOOL_TIMEOUT", error_type, detail, True
    if isinstance(exc, LookupError):
        return "TOOL_NOT_FOUND", error_type, detail, False
    if isinstance(exc, ValueError):
        return "TOOL_ARGUMENT_INVALID", error_type, detail, False
    if (
        tool_name == "database_query"
        or "sql" in lowered_type
        or "database" in lowered_type
    ):
        return "TOOL_DATABASE_ERROR", error_type, detail, True
    if any(token in lowered_type for token in ("http", "client", "connection")):
        return "TOOL_UPSTREAM_ERROR", error_type, detail, True
    return "TOOL_EXECUTION_FAILED", error_type, detail, failure_stage == "execution"


def _semantic_tool_error(
    tool_name: str, observation: str
) -> Optional[tuple[str, str, str, bool]]:
    try:
        parsed = json.loads(observation)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("success") is not False:
        return None
    error = parsed.get("error")
    if isinstance(error, dict):
        error_type = str(error.get("type") or "ToolReportedFailure")
        error_message = str(error.get("message") or error_type)
        retryable = bool(error.get("retryable", False)) or (
            tool_name == "database_query"
            and (
                error_type.lower() == "connection"
                or "timeout" in error_message.lower()
                or "超时" in error_message
            )
        )
    else:
        error_type = "ToolReportedFailure"
        error_message = str(error or "工具返回失败状态")
        retryable = False
    error_code = (
        "TOOL_DATABASE_ERROR"
        if tool_name == "database_query"
        else "TOOL_EXECUTION_FAILED"
    )
    return error_code, error_type, error_message, retryable


class AgentLoop:
    def __init__(
        self,
        llm: HelloAgentsLLM,
        tools: ToolExecutor,
        skills: Optional[SkillCatalog] = None,
    ) -> None:
        settings = get_settings()
        self.llm = llm
        self.tools = tools
        self.skills = skills
        self.context_service = ContextService()
        self.max_rounds = settings.react.max_rounds
        self.force_answer = settings.react.force_answer_on_last_round

    @property
    def model_name(self) -> str:
        return self.llm.model

    async def close(self) -> None:
        await self.tools.close()

    async def run_stream(
        self,
        question: str,
        history: list[ChatMessage],
        run_id: str = "-",
        *,
        frontend_context: FrontendContext | None = None,
        audit_recorder: ToolAuditRecorder | None = None,
    ) -> AsyncIterator[AgentEvent]:
        limit = get_settings().conversation.max_history_messages
        tool_definitions = self.tools.definitions()
        skill_summaries = self.skills.summaries() if self.skills else ()
        messages = self.context_service.build_messages(
            question=question,
            history=history,
            frontend_context=frontend_context,
            skills=skill_summaries,
            max_history_messages=limit,
        )
        citation_registry = CitationRegistry()
        observation_map_rendered = False
        has_streamed_content = False
        tool_sequence = 0
        logger.info(
            "agent_started run_id=%s question=%r history=%r",
            run_id,
            question,
            history,
        )

        for round_number in range(1, self.max_rounds + 1):
            round_started_at = perf_counter()
            is_forced_final_round = (
                self.force_answer and round_number == self.max_rounds
            )
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            has_answer_content = False
            citation_buffer = ""
            round_has_streamed_content = False
            available_tools = (
                None
                if is_forced_final_round or not tool_definitions
                else tool_definitions
            )
            round_messages = self.context_service.build_round_messages(
                messages,
                round_number=round_number,
                max_rounds=self.max_rounds,
                is_forced_final_round=is_forced_final_round,
            )
            logger.info(
                "llm_round_started run_id=%s round=%s messages=%r tools=%r",
                run_id,
                round_number,
                round_messages,
                [
                    item.get("function", {}).get("name")
                    for item in available_tools or []
                ],
            )

            async for content, calls in self.llm.stream(
                round_messages,
                available_tools,
            ):
                if content:
                    has_answer_content = has_answer_content or bool(content.strip())
                    content_parts.append(content)
                    if citation_registry.has_sources:
                        citation_buffer += content
                        content, citation_buffer = (
                            citation_registry.transform_stream_buffer(
                                citation_buffer,
                                final=False,
                            )
                        )
                    if content:
                        if has_streamed_content and not round_has_streamed_content:
                            yield AgentEvent(
                                type="answer_delta",
                                content="\n\n",
                                round_number=round_number,
                            )
                        round_has_streamed_content = True
                        has_streamed_content = True
                        yield AgentEvent(
                            type="answer_delta",
                            content=content,
                            round_number=round_number,
                        )
                if calls:
                    tool_calls.extend(calls)

            if citation_registry.has_sources:
                tail, citation_buffer = citation_registry.transform_stream_buffer(
                    citation_buffer,
                    final=True,
                )
                if citation_buffer:
                    raise RuntimeError("模型未生成有效回答")
                if tail:
                    if has_streamed_content and not round_has_streamed_content:
                        yield AgentEvent(
                            type="answer_delta",
                            content="\n\n",
                            round_number=round_number,
                        )
                    has_streamed_content = True
                    yield AgentEvent(
                        type="answer_delta",
                        content=tail,
                        round_number=round_number,
                    )

            content = "".join(content_parts)
            normalized_calls: list[dict[str, Any]] = []
            for index, tool_call in enumerate(tool_calls):
                name = str(tool_call.get("name") or "")
                if not name:
                    raise RuntimeError("模型返回了无效工具调用")
                normalized_calls.append(
                    {
                        **tool_call,
                        "id": str(
                            tool_call.get("id") or f"call-{round_number}-{index}"
                        ),
                        "name": name,
                        "args": tool_call.get("args") or {},
                        "type": "tool_call",
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": normalized_calls,
                }
            )
            logger.info(
                "llm_round_completed run_id=%s round=%s duration_ms=%.1f "
                "content=%r tool_calls=%r",
                run_id,
                round_number,
                (perf_counter() - round_started_at) * 1000,
                content,
                normalized_calls,
            )

            if not normalized_calls:
                if not has_answer_content:
                    raise RuntimeError("模型未生成有效回答")
                logger.info(
                    "agent_completed run_id=%s round=%s reason=stop",
                    run_id,
                    round_number,
                )
                return

            if round_number == self.max_rounds:
                if not has_answer_content:
                    raise RuntimeError("AgentLoop 达到最大轮数但未生成回答")
                logger.info(
                    "agent_completed run_id=%s round=%s reason=max_rounds",
                    run_id,
                    round_number,
                )
                return

            for tool_call in normalized_calls:
                name = tool_call["name"]
                tool_sequence += 1
                if audit_recorder is not None:
                    await audit_recorder.started(
                        tool_call_id=tool_call["id"],
                        round_number=round_number,
                        sequence_no=tool_sequence,
                        tool_name=name,
                        arguments=tool_call["args"],
                    )
                tool_started_at = perf_counter()
                logger.info(
                    "tool_started run_id=%s round=%s tool=%s call_id=%s "
                    "arguments=%r",
                    run_id,
                    round_number,
                    name,
                    tool_call["id"],
                    tool_call["args"],
                )
                yield AgentEvent(
                    type="tool_started",
                    round_number=round_number,
                    tool_name=name,
                    tool_call_id=tool_call["id"],
                )
                failure_stage = "execution"
                error_code = None
                error_type = None
                error_message = None
                retryable = False
                try:
                    observation, client_events = await self.tools.execute_with_events(
                        name, tool_call["args"]
                    )
                    failure_stage = "result_processing"
                    tool_arguments = tool_call["args"]
                    if name == "rag_search":
                        rag_result = citation_registry.register_rag_observation(
                            observation,
                            tool_call_id=tool_call["id"],
                            fallback_query=(
                                tool_arguments.get("query")
                                if isinstance(tool_arguments, dict)
                                else ""
                            ),
                        )
                        retrieval_data, observation = rag_result
                    else:
                        retrieval_data = None
                    if name == "observation_service" and client_events:
                        observation_map_rendered = True
                    elif name == "map_action" and observation_map_rendered and any(
                        event.get("action_type") == "FLY_TO"
                        for event in client_events
                    ):
                        client_events = ()
                    semantic_error = _semantic_tool_error(name, observation)
                    if semantic_error is None:
                        success = True
                        failure_stage = None
                    else:
                        success = False
                        (
                            error_code,
                            error_type,
                            error_message,
                            retryable,
                        ) = semantic_error
                        failure_stage = "execution"
                except asyncio.CancelledError:
                    duration_ms = max(
                        0, int((perf_counter() - tool_started_at) * 1000)
                    )
                    if audit_recorder is not None:
                        await audit_recorder.finished(
                            tool_call_id=tool_call["id"],
                            status="interrupted",
                            result="工具调用被取消",
                            duration_ms=duration_ms,
                            error_code="AGENT_INTERRUPTED",
                            error_type="CancelledError",
                            error_message="服务关闭导致工具调用被取消",
                            failure_stage=failure_stage,
                            retryable=True,
                        )
                    raise
                except Exception as exc:
                    logger.exception(
                        "tool_failed run_id=%s round=%s tool=%s call_id=%s "
                        "duration_ms=%.1f arguments=%r exception_type=%s",
                        run_id,
                        round_number,
                        name,
                        tool_call["id"],
                        (perf_counter() - tool_started_at) * 1000,
                        tool_call["args"],
                        type(exc).__name__,
                    )
                    if name == "observation_service" and isinstance(
                        exc, ValueError
                    ):
                        observation = (
                            "观测工具参数无效："
                            f"{exc}。请严格使用 observation_service schema 中定义的 "
                            "operation 和参数；如果现有操作无法完成问题，应明确说明能力不足，"
                            "不得编造新的 operation。"
                        )
                    elif name == "observation_service":
                        detail = str(exc).strip() or type(exc).__name__
                        observation = f"观测服务调用失败：{detail}"
                    else:
                        observation = "工具执行失败"
                    retrieval_data = None
                    client_events = ()
                    success = False
                    (
                        error_code,
                        error_type,
                        error_message,
                        retryable,
                    ) = _classify_tool_error(
                        exc,
                        tool_name=name,
                        failure_stage=failure_stage,
                    )

                duration_ms = max(
                    0, int((perf_counter() - tool_started_at) * 1000)
                )
                if audit_recorder is not None:
                    await audit_recorder.finished(
                        tool_call_id=tool_call["id"],
                        status="succeeded" if success else "failed",
                        result=observation,
                        duration_ms=duration_ms,
                        error_code=error_code,
                        error_type=error_type,
                        error_message=error_message,
                        failure_stage=failure_stage,
                        retryable=retryable,
                    )

                if success:
                    logger.info(
                        "tool_completed run_id=%s round=%s tool=%s call_id=%s "
                        "duration_ms=%.1f result=%r client_events=%r",
                        run_id,
                        round_number,
                        name,
                        tool_call["id"],
                        duration_ms,
                        observation,
                        client_events,
                    )

                if retrieval_data is not None:
                    yield AgentEvent(
                        type="retrieval",
                        data=retrieval_data,
                        sources=citation_registry.sources(),
                        round_number=round_number,
                        tool_name=name,
                        tool_call_id=tool_call["id"],
                    )
                for action_sequence, client_event in enumerate(
                    client_events, start=1
                ):
                    yield AgentEvent(
                        type="map_action",
                        data={**client_event, "seq": action_sequence},
                        round_number=round_number,
                        tool_name=name,
                        tool_call_id=tool_call["id"],
                    )
                yield AgentEvent(
                    type="tool_finished",
                    content=observation,
                    round_number=round_number,
                    tool_name=name,
                    tool_call_id=tool_call["id"],
                    success=success,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": name,
                        "content": observation,
                    }
                )


def build_agent_loop() -> AgentLoop:
    skills = build_skill_catalog()
    return AgentLoop(
        HelloAgentsLLM(),
        build_tool_executor(skills),
        skills,
    )
