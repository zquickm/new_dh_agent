from __future__ import annotations

import json
import sys
import traceback
from collections.abc import AsyncIterator
from textwrap import dedent
from typing import Any, Optional

from agents.events import AgentEvent
from agents.llm_client import HelloAgentsLLM
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_tool_executor
from config.settings import get_settings
from schemas.chat import ChatMessage


SYSTEM_PROMPT = dedent(
    """
    你是一个可以调用工具完成任务的智能助手。

    工作规则：
    - 需要实时信息、业务数据或内部文档时，调用合适的工具。
    - 可以根据任务需要连续调用多个工具。
    - 工具返回结果后，继续判断是否还需要调用工具。
    - 信息足够时，直接回答用户，不要描述内部执行过程。
    - 不得编造工具返回的数据。
    """
).strip()

_BACKEND_TOOL_NAMES = {"database_query", "rag_search"}
_INFRASTRUCTURE_MODULES = {
    "aiohttp",
    "aiomysql",
    "asyncmy",
    "httpcore",
    "httpx",
    "pymysql",
}


def _rag_retrieval_data(
    observation: str,
    *,
    tool_call_id: str,
    fallback_query: Any,
) -> dict[str, Any]:
    try:
        result = json.loads(observation)
    except json.JSONDecodeError as exc:
        raise ValueError("rag_search returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("chunks"), list):
        raise ValueError("rag_search returned an invalid result")

    chunks: list[dict[str, Any]] = []
    for rank, item in enumerate(result["chunks"], start=1):
        if not isinstance(item, dict):
            raise ValueError("rag_search returned an invalid chunk")
        text = item.get("text")
        source = item.get("source")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("rag_search returned a chunk without text")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("rag_search returned a chunk without source")
        chunks.append(
            {
                "rank": rank,
                "source": source,
                "preview": text,
                "score": item.get("score"),
                "document_id": item.get("document_id"),
                "chunk_id": item.get("chunk_id"),
                "knowledge_id": item.get("knowledge_id"),
                "title": item.get("title"),
                "url": item.get("url"),
                "page_index": item.get("page_index"),
                "line": item.get("line"),
                "page": item.get("page"),
            }
        )

    query = result.get("query")
    if not isinstance(query, str):
        query = str(fallback_query or "")
    return {
        "tool_call_id": tool_call_id,
        "tool_name": "rag_search",
        "query": query,
        "result_count": len(chunks),
        "chunks": chunks,
    }


def _is_serious_tool_error(exc: Exception) -> bool:
    current: Optional[BaseException] = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        error_name = type(current).__name__
        module_root = type(current).__module__.partition(".")[0]
        if error_name in {"DatabaseToolError", "RagToolError"}:
            current = current.__cause__ or current.__context__
            continue
        if isinstance(
            current,
            (ConnectionError, TimeoutError, OSError, AttributeError, TypeError, KeyError),
        ):
            return True
        if module_root in _INFRASTRUCTURE_MODULES:
            return True
        if isinstance(current, (ValueError, LookupError)) or module_root == "sqlglot":
            current = current.__cause__ or current.__context__
            continue
        return True
    return False


def _print_serious_tool_error(tool_name: str, exc: Exception) -> None:
    if tool_name not in _BACKEND_TOOL_NAMES or not _is_serious_tool_error(exc):
        return
    print(f"Serious error while executing tool {tool_name}:", file=sys.stdout)
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stdout)
    sys.stdout.flush()


class AgentLoop:
    def __init__(self, llm: HelloAgentsLLM, tools: ToolExecutor) -> None:
        settings = get_settings()
        self.llm = llm
        self.tools = tools
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
    ) -> AsyncIterator[AgentEvent]:
        limit = get_settings().conversation.max_history_messages
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *[
                {"role": item.role, "content": item.content}
                for item in history[-limit:]
            ],
            {"role": "user", "content": question},
        ]
        tool_definitions = self.tools.definitions()

        for round_number in range(1, self.max_rounds + 1):
            available_tools = (
                None
                if self.force_answer and round_number == self.max_rounds
                else tool_definitions or None
            )
            content_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []

            async for content, calls in self.llm.stream(messages, available_tools):
                if content:
                    content_parts.append(content)
                if calls:
                    tool_calls.extend(calls)

            content = "".join(content_parts)
            if not tool_calls:
                if not content.strip():
                    raise RuntimeError("模型未生成有效回答")
                for part in content_parts:
                    yield AgentEvent(
                        type="answer_delta",
                        content=part,
                        round_number=round_number,
                    )
                return

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

            for tool_call in normalized_calls:
                name = tool_call["name"]
                yield AgentEvent(
                    type="tool_started",
                    round_number=round_number,
                    tool_name=name,
                    tool_call_id=tool_call["id"],
                )
                try:
                    observation = await self.tools.execute(name, tool_call["args"])
                    tool_arguments = tool_call["args"]
                    retrieval_data = (
                        _rag_retrieval_data(
                            observation,
                            tool_call_id=tool_call["id"],
                            fallback_query=(
                                tool_arguments.get("query")
                                if isinstance(tool_arguments, dict)
                                else ""
                            ),
                        )
                        if name == "rag_search"
                        else None
                    )
                    success = True
                except Exception as exc:
                    _print_serious_tool_error(name, exc)
                    observation = "工具执行失败"
                    retrieval_data = None
                    success = False

                if retrieval_data is not None:
                    yield AgentEvent(
                        type="retrieval",
                        data=retrieval_data,
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

        raise RuntimeError("AgentLoop 达到最大轮数但未生成回答")


def build_agent_loop() -> AgentLoop:
    return AgentLoop(HelloAgentsLLM(), build_tool_executor())
