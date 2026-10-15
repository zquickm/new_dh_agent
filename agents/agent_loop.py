from __future__ import annotations

import asyncio
import json
import logging
import inspect
from collections.abc import AsyncIterator
from copy import deepcopy
from time import perf_counter
from typing import Any, Callable, Optional

from agents.citations import CitationRegistry
from agents.llm_diagnostics import observation_log_summary
from agents.events import AgentEvent
from agents.llm_client import HelloAgentsLLM
from agents.skills import SkillCatalog
from agents.tool_audit import ToolAuditRecorder
from agents.temporary_suspicion_scope import restrict_request
from agents.tools.executor import ToolExecutor
from agents.tools.map_action import spatial_map_results
from agents.tools.context import ToolContext
from agents.tools.registry import build_skill_catalog, build_tool_executor
from config.settings import get_settings
from schemas.chat import ChatMessage, FrontContext, FrontendContext
from services.context_service import ContextService, SYSTEM_PROMPT
from services.rag_debug_service import rag_debug_scope
from services.business_scenario import resolve_rag_scenario
from services.token_budget_service import (
    DeepSeekV4TokenCounter,
    TOKENIZER_NAME,
    TokenBudgetService,
)


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
    if tool_name == "load_skill" and isinstance(exc, PermissionError):
        return "SKILL_UNAVAILABLE", error_type, detail, False
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
        self.temperature = settings.llm.temperature
        self.context_window_tokens = settings.llm.context_window_tokens
        self.max_output_tokens = settings.llm.max_output_tokens
        self.token_safety_margin = settings.llm.token_safety_margin
        self.input_budget_tokens = settings.llm.input_budget_tokens
        self.token_budget = TokenBudgetService(
            DeepSeekV4TokenCounter(settings.llm.resolved_tokenizer_path()),
            self.input_budget_tokens,
        )

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
        front_context: FrontContext | None = None,
        audit_recorder: ToolAuditRecorder | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        enabled_skill_names: frozenset[str] | None = None,
        context_enabled: bool = True,
        context_message_limit: int | None = None,
        context_token_limit: int | None = None,
        context_usage_callback: Callable[[dict[str, Any]], Any] | None = None,
        model_messages_callback: Callable[[list[dict[str, Any]]], Any] | None = None,
        tool_progress_callback: Callable[[dict[str, Any]], Any] | None = None,
        rag_debug_callback: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        configured_limit = get_settings().conversation.max_history_messages
        limit = min(context_message_limit or configured_limit, configured_limit)
        if not context_enabled:
            limit = 0
        window_tokens = min(
            context_token_limit or self.context_window_tokens,
            self.context_window_tokens,
        )
        input_budget_tokens = window_tokens - self.max_output_tokens - self.token_safety_margin
        if input_budget_tokens <= 0:
            raise ValueError("会话上下文上限不足以容纳输出预留")
        budget_service = (
            self.token_budget
            if input_budget_tokens == self.input_budget_tokens
            else TokenBudgetService(self.token_budget.counter, input_budget_tokens)
        )
        rag_scenario = resolve_rag_scenario(front_context)
        logger.info(
            "agent_submodule_selected run_id=%s front_context=%r base_rag_scenario=%s",
            run_id, front_context.model_dump() if front_context is not None else None, rag_scenario,
        )
        tools = self.tools
        skill_summaries = self.skills.summaries() if self.skills else ()
        # 临时限制接入：移除此调用及导入即可恢复全量能力。
        tools, history, enabled_skill_names, system_prompt = restrict_request(
            question, tools, history, enabled_skill_names, system_prompt,
            available_skill_names=frozenset(skill.name for skill in skill_summaries),
        )
        tool_definitions = tools.definitions()
        history = self.context_service.filter_rag_history(history)
        if enabled_skill_names is not None:
            enabled_skill_names = frozenset(enabled_skill_names)
            skill_summaries = tuple(
                skill for skill in skill_summaries if skill.name in enabled_skill_names
            )
            history = self.context_service.filter_skill_history(history, enabled_skill_names)
        all_history_groups = self.context_service.history_groups(history, len(history))
        history_groups = self.context_service.history_groups(history, limit)
        history_group_sizes = tuple(len(group) for group in history_groups)
        original_history_count = sum(len(group) for group in all_history_groups)
        messages = self.context_service.build_messages(
            question=question,
            history=history,
            frontend_context=frontend_context,
            skills=skill_summaries,
            max_history_messages=limit,
            system_prompt=system_prompt,
            rag_scenario=rag_scenario,
        )
        input_message_count = len(messages)
        cumulative_input_tokens = 0
        cumulative_output_tokens = 0
        latest_usage: dict[str, Any] = {}
        cumulative_tool_results_truncated = 0
        citation_registry = CitationRegistry()
        has_streamed_content = False
        tool_sequence = 0
        map_results: dict[str, dict[str, Any]] = {}
        model_messages: list[dict[str, Any]] = []

        async def save_model_messages(batch: list[dict[str, Any]]) -> None:
            # Save only complete batches; interruption cannot leave orphan calls.
            model_messages.extend(
                item.model_dump(exclude_none=True)
                for item in self.context_service.filter_rag_history(
                    [ChatMessage.model_validate(item) for item in batch]
                )
            )
            if model_messages_callback is not None:
                result = model_messages_callback(deepcopy(model_messages))
                if inspect.isawaitable(result):
                    await result

        logger.info(
            "agent_started run_id=%s history_count=%d",
            run_id,
            len(history),
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
            fitted = budget_service.fit(
                round_messages,
                history_group_sizes=history_group_sizes,
                original_history_count=original_history_count,
                tools=available_tools,
            )
            cumulative_tool_results_truncated += fitted.tool_results_truncated
            history_group_sizes = fitted.history_group_sizes
            messages = [{**messages[0]}, *fitted.messages[1:]]
            input_message_count = 1 + fitted.history_messages_included + 1
            round_messages = fitted.messages
            latest_usage = {
                "context_window_tokens": window_tokens,
                "input_budget_tokens": input_budget_tokens,
                "estimated_input_tokens": fitted.estimated_input_tokens,
                "input_tokens": fitted.estimated_input_tokens,
                "output_tokens": 0,
                "used_tokens": fitted.estimated_input_tokens,
                "usage_percent": round(fitted.estimated_input_tokens * 100 / window_tokens, 2),
                "history_messages_included": fitted.history_messages_included,
                "history_messages_dropped": fitted.history_messages_dropped,
                "tool_results_truncated": cumulative_tool_results_truncated,
                "tokenizer": TOKENIZER_NAME,
                "round_number": round_number,
                "source": "estimated",
                "cumulative_input_tokens": cumulative_input_tokens,
                "cumulative_output_tokens": cumulative_output_tokens,
                "cumulative_total_tokens": cumulative_input_tokens + cumulative_output_tokens,
            }
            if context_usage_callback is not None:
                callback_result = context_usage_callback(latest_usage)
                if inspect.isawaitable(callback_result):
                    await callback_result
            logger.info(
                "llm_round_started run_id=%s round=%s message_sizes=%r tools=%r",
                run_id,
                round_number,
                [(m.get("role"), len(str(m.get("content") or ""))) for m in round_messages],
                [
                    item.get("function", {}).get("name")
                    for item in available_tools or []
                ],
            )

            stream_kwargs = {"temperature": self.temperature}
            stream_parameters = inspect.signature(self.llm.stream).parameters
            if "run_id" in stream_parameters:
                stream_kwargs.update({"run_id": run_id, "round_number": round_number})
            upstream_usage: dict[str, int] = {}
            if "usage_callback" in stream_parameters:
                stream_kwargs["usage_callback"] = upstream_usage.update
            reasoning_chunks: list[str] = []
            if "reasoning_callback" in stream_parameters:
                stream_kwargs["reasoning_callback"] = reasoning_chunks.append
            async for content, calls in self.llm.stream(
                round_messages,
                available_tools,
                **stream_kwargs,
            ):
                for reasoning_content in reasoning_chunks:
                    yield AgentEvent(type="reasoning_delta", content=reasoning_content, round_number=round_number)
                reasoning_chunks.clear()
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
                            sources=citation_registry.sources(display_ids=True),
                            round_number=round_number,
                        )
                if calls:
                    tool_calls.extend(calls)

            if upstream_usage:
                actual_input = int(upstream_usage.get("input_tokens") or 0)
                actual_output = int(upstream_usage.get("output_tokens") or 0)
                cumulative_input_tokens += actual_input
                cumulative_output_tokens += actual_output
                latest_usage = {
                    **latest_usage,
                    "input_tokens": actual_input,
                    "output_tokens": actual_output,
                    "used_tokens": actual_input + actual_output,
                    "usage_percent": round((actual_input + actual_output) * 100 / window_tokens, 2),
                    "source": "upstream",
                    "cumulative_input_tokens": cumulative_input_tokens,
                    "cumulative_output_tokens": cumulative_output_tokens,
                    "cumulative_total_tokens": cumulative_input_tokens + cumulative_output_tokens,
                }
                if context_usage_callback is not None:
                    callback_result = context_usage_callback(latest_usage)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            else:
                estimated_output = self.token_budget.counter.count_text(
                    "".join(content_parts)
                )
                cumulative_input_tokens += fitted.estimated_input_tokens
                cumulative_output_tokens += estimated_output
                latest_usage = {
                    **latest_usage,
                    "output_tokens": estimated_output,
                    "used_tokens": fitted.estimated_input_tokens + estimated_output,
                    "usage_percent": round(
                        (fitted.estimated_input_tokens + estimated_output)
                        * 100
                        / window_tokens,
                        2,
                    ),
                    "cumulative_input_tokens": cumulative_input_tokens,
                    "cumulative_output_tokens": cumulative_output_tokens,
                    "cumulative_total_tokens": (
                        cumulative_input_tokens + cumulative_output_tokens
                    ),
                }
                if context_usage_callback is not None:
                    callback_result = context_usage_callback(latest_usage)
                    if inspect.isawaitable(callback_result):
                        await callback_result

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
                        sources=citation_registry.sources(display_ids=True),
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
                "content_chars=%d tool_names=%r",
                run_id,
                round_number,
                (perf_counter() - round_started_at) * 1000,
                len(content),
                [call["name"] for call in normalized_calls],
            )

            if not normalized_calls:
                if not has_answer_content:
                    raise RuntimeError("模型未生成有效回答")
                await save_model_messages([{"role": "assistant", "content": content}])
                logger.info(
                    "agent_completed run_id=%s round=%s reason=no_tool_calls",
                    run_id,
                    round_number,
                )
                return

            if round_number == self.max_rounds:
                if not has_answer_content:
                    raise RuntimeError("AgentLoop 达到最大轮数但未生成回答")
                # These calls will not execute, so retain only the answer text.
                await save_model_messages([{"role": "assistant", "content": content}])
                logger.info(
                    "agent_completed run_id=%s round=%s reason=max_rounds",
                    run_id,
                    round_number,
                )
                return

            round_messages_to_save = [deepcopy(messages[-1])]
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
                registered_tool = tools.get(name)
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
                audit_observation = ""
                async def on_rag_debug(stage, data):
                    if rag_debug_callback is not None:
                        await rag_debug_callback(stage, {**data, "tool_call_id": tool_call["id"],
                            "round_number": round_number,
                            **({"original_question": question} if stage == "query" else {})})
                try:
                    token = spatial_map_results.set(map_results)
                    try:
                        async def on_progress(data):
                            if tool_progress_callback is not None:
                                await tool_progress_callback({
                                    **data,
                                    "step_id": f"{tool_call['id']}:{data['step_id']}",
                                    "parent_id": tool_call["id"],
                                    "round_number": round_number,
                                })

                        execute_kwargs = {"enabled_skill_names": enabled_skill_names}
                        if registered_tool and registered_tool.contextual:
                            execute_kwargs["context"] = ToolContext(on_progress=on_progress, rag_scenario=rag_scenario)
                        with rag_debug_scope(on_rag_debug if name == "rag_search" and rag_debug_callback is not None else None):
                            observation, client_events = await tools.execute_with_events(
                                name, tool_call["args"], **execute_kwargs
                            )
                    finally:
                        # Reset before yielding, including failure/cancellation paths.
                        spatial_map_results.reset(token)
                    audit_observation = observation
                    failure_stage = "result_processing"
                    semantic_error = _semantic_tool_error(
                        name, audit_observation or observation
                    )
                    if semantic_error is None:
                        tool_arguments = tool_call["args"]
                        if name == "rag_search":
                            retrieval_data, observation = citation_registry.register_rag_observation(
                                observation,
                                tool_call_id=tool_call["id"],
                                fallback_query=(
                                    tool_arguments.get("query")
                                    if isinstance(tool_arguments, dict)
                                    else ""
                                ),
                            )
                            await on_rag_debug("sources", {"chunks": [
                                {key: chunk.get(key) for key in ("rank", "buffer_id", "knowledge_id", "document_id", "chunk_id", "source")}
                                for chunk in retrieval_data["chunks"]
                            ]})
                        else:
                            retrieval_data = None
                        success = True
                        failure_stage = None
                    else:
                        retrieval_data = None
                        success = False
                        (
                            error_code,
                            error_type,
                            error_message,
                            retryable,
                        ) = semantic_error
                        failure_stage = "execution"
                except asyncio.CancelledError as exc:
                    if name == "rag_search":
                        await on_rag_debug("tool_end", {"status": "cancelled", "complete": False})
                    duration_ms = max(
                        0, int((perf_counter() - tool_started_at) * 1000)
                    )
                    user_cancelled = "USER_CANCELLED" in exc.args
                    if audit_recorder is not None:
                        await audit_recorder.finished(
                            tool_call_id=tool_call["id"],
                            status="interrupted",
                            result=(
                                "用户停止生成导致工具调用被取消"
                                if user_cancelled else "工具调用被取消"
                            ),
                            duration_ms=duration_ms,
                            error_code=(
                                "USER_CANCELLED"
                                if user_cancelled else "AGENT_INTERRUPTED"
                            ),
                            error_type=(
                                "UserCancelled"
                                if user_cancelled else "CancelledError"
                            ),
                            error_message=(
                                "用户停止生成导致工具调用被取消"
                                if user_cancelled else "服务关闭导致工具调用被取消"
                            ),
                            failure_stage=failure_stage,
                            retryable=not user_cancelled,
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
                    if name == "load_skill" and isinstance(exc, PermissionError):
                        observation = "该 Skill 在本次任务中不可用，请使用当前可用列表中的 Skill；业务工具仍可正常使用。"
                    elif name == "spatial_query_service":
                        arguments = tool_call["args"]
                        operation = arguments.get("operation") if isinstance(arguments, dict) else None
                        service = "通用空间查询服务"
                        context = f"（operation={operation}）" if operation else ""
                        detail = str(exc).strip() or type(exc).__name__
                        if isinstance(exc, ValueError):
                            observation = (
                                f"{service}参数或返回结果无效{context}：{detail}。"
                                "请核对 spatial_query_service schema 中的 operation、参数及服务返回结果；"
                                "如果现有操作无法完成问题，应明确说明能力不足，不得编造新的 operation。"
                            )
                        else:
                            observation = f"{service}调用失败{context}：{detail}"
                    elif name == "dm_duanmian_analysis" and isinstance(exc, ValueError):
                        observation = f"断面专题分析参数无效：{exc}"
                    elif name == "dm_duanmian_element_compare" and isinstance(exc, (ValueError, TypeError)):
                        observation = f"断面要素对比参数无效：{exc}"
                    elif name == "map_action" and isinstance(exc, ValueError):
                        observation = f"地图操作参数无效：{exc}"
                    else:
                        observation = "工具执行失败"
                    if not audit_observation:
                        audit_observation = observation
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
                if name == "rag_search":
                    await on_rag_debug("tool_end", {"status": "succeeded" if success else "failed",
                        "complete": success, "duration_ms": duration_ms, "error_code": error_code,
                        "error_type": error_type, "failure_stage": failure_stage})
                if audit_recorder is not None:
                    await audit_recorder.finished(
                        tool_call_id=tool_call["id"],
                        status="succeeded" if success else "failed",
                        result=audit_observation or observation,
                        duration_ms=duration_ms,
                        error_code=error_code,
                        error_type=error_type,
                        error_message=error_message,
                        failure_stage=failure_stage,
                        retryable=retryable,
                    )

                if success and name == "database_query":
                    citation_registry.register_database_observation(
                        observation, question=str(tool_call["args"].get("question", ""))
                    )
                if success:
                    logger.info(
                        "tool_completed run_id=%s round=%s tool=%s call_id=%s "
                        "duration_ms=%.1f result_chars=%d client_event_count=%d summary=%r",
                        run_id,
                        round_number,
                        name,
                        tool_call["id"],
                        duration_ms,
                        len(observation),
                        len(client_events),
                        observation_log_summary(audit_observation or observation),
                    )

                if retrieval_data is not None:
                    yield AgentEvent(
                        type="retrieval",
                        data=retrieval_data,
                        sources=citation_registry.sources(display_ids=True),
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
                    if (
                        name == "map_action"
                        and client_event.get("action_type") == "CLEAR"
                        and client_event.get("clear_selection") is True
                    ):
                        # Keep tool messages; replace only the original input context.
                        messages[:input_message_count] = self.context_service.build_messages(
                            question=question,
                            history=history,
                            frontend_context=FrontendContext(),
                            skills=skill_summaries,
                            max_history_messages=limit,
                            system_prompt=system_prompt,
                            rag_scenario=rag_scenario,
                        )
                        history_group_sizes = tuple(
                            len(group)
                            for group in self.context_service.history_groups(history, limit)
                        )
                yield AgentEvent(
                    type="tool_finished",
                    content=observation,
                    round_number=round_number,
                    tool_name=name,
                    tool_call_id=tool_call["id"],
                    success=success,
                    sources=citation_registry.sources(display_ids=True),
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "name": name,
                        "content": observation,
                    }
                )
                round_messages_to_save.append(deepcopy(messages[-1]))
            await save_model_messages(round_messages_to_save)


def build_agent_loop() -> AgentLoop:
    skills = build_skill_catalog()
    return AgentLoop(
        HelloAgentsLLM(),
        build_tool_executor(skills),
        skills,
    )
