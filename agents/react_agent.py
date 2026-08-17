from __future__ import annotations

import json
import re
import sys
import traceback
from collections.abc import AsyncIterator
from textwrap import dedent
from typing import Optional

from agents.events import ActionResult, ReActEvent
from agents.llm_client import HelloAgentsLLM
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_tool_executor
from config.settings import get_settings
from schemas.chat import ChatMessage


_BACKEND_TOOL_NAMES = {"database_query", "rag_search"}
_INFRASTRUCTURE_MODULES = {
    "aiohttp",
    "aiomysql",
    "asyncmy",
    "httpcore",
    "httpx",
    "pymysql",
}


def _is_serious_tool_error(exc: Exception) -> bool:
    """Distinguish infrastructure/programming failures from business errors."""
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


def build_prompt(
    template: str,
    *,
    context: Optional[list[str]] = None,
    **values: object,
) -> str:
    prompt = dedent(template).strip()
    if values:
        prompt = prompt.format(**values)
    if context is None:
        return prompt
    context_text = "\n".join(context) or "无"
    return f"当前上下文：\n----------------\n{context_text}\n----------------\n\n{prompt}"


class ThinkAgent:
    PROMPT = build_prompt(
        """
        任务：概括当前问题的处理进展，并说明下一步需要什么信息。

        要求：
        - 简短、清楚，可以直接展示给用户。
        - 只输出推理概括，不展示详细思维过程。
        - 不调用工具，不直接给出最终答案。
        """
    )

    def __init__(self, llm: HelloAgentsLLM) -> None:
        self.llm = llm

    async def run(self, context: list[str]) -> AsyncIterator[str]:
        async for token in self.llm.stream(
            [{"role": "user", "content": build_prompt(self.PROMPT, context=context)}]
        ):
            yield token


class ActionAgent:
    def __init__(self, llm: HelloAgentsLLM, tools: ToolExecutor) -> None:
        self.llm = llm
        self.tools = tools

    async def run(
        self,
        context: list[str],
        preferred_tool: Optional[str] = None,
    ) -> ActionResult:
        if preferred_tool:
            try:
                observation = await self.tools.execute(preferred_tool, "")
                return ActionResult(preferred_tool, "", observation, True)
            except Exception as exc:
                _print_serious_tool_error(preferred_tool, exc)
                return ActionResult(preferred_tool, "", "工具执行失败", False)
        instruction = build_prompt(
            """
            任务：根据完整上下文选择最适合当前步骤的一个工具。

            可用工具：
            {tools}

            输出要求：
            - 严格按照工具的 parameters 生成 JSON 参数对象。
            - 完整保留用户给出的时间、断面、站点、指标和过滤条件。
            - 只输出：工具名称[JSON参数对象]
            - 不要输出 Markdown、解释或最终答案。
            """,
            tools=self.tools.descriptions(),
        )
        prompt = build_prompt(instruction, context=context)
        action_text = await self.llm.invoke(
            [{"role": "user", "content": prompt}], temperature=0
        )
        match = re.search(r"([\w.-]+)\[(.*)\]", action_text.strip(), re.DOTALL)
        if not match:
            return ActionResult("", "", "模型未返回有效工具调用", False)
        name, argument = match.group(1), match.group(2).strip()
        try:
            observation = await self.tools.execute(name, argument)
            return ActionResult(name, argument, observation, True)
        except Exception as exc:
            _print_serious_tool_error(name, exc)
            return ActionResult(name, argument, "工具执行失败", False)


class AnswerAgent:
    PROMPT = build_prompt(
        """
        任务：根据上下文直接回答用户的问题。

        要求：
        - 合理使用思考概括和工具返回的信息。
        - 不要输出内部思考和调用工具的过程。
        """
    )

    def __init__(self, llm: HelloAgentsLLM) -> None:
        self.llm = llm

    async def run(self, context: list[str]) -> AsyncIterator[str]:
        async for token in self.llm.stream(
            [{"role": "user", "content": build_prompt(self.PROMPT, context=context)}]
        ):
            yield token


class CoreAgent:
    ROUTE_PROMPT = build_prompt(
        """
        请快速选择下一步：
        - think：分析梳理。
        - action：调用工具。
        - answer：给出最终回答。

        只输出 JSON，例如 {"agent": "think"}。
        """
    )

    def __init__(self, llm: HelloAgentsLLM, tools: ToolExecutor) -> None:
        settings = get_settings()
        self.llm = llm
        self.tools = tools
        self.max_rounds = settings.react.max_rounds
        self.force_answer = settings.react.force_answer_on_last_round
        self.think_agent = ThinkAgent(llm)
        self.action_agent = ActionAgent(llm, tools)
        self.answer_agent = AnswerAgent(llm)

    @property
    def model_name(self) -> str:
        return self.llm.model

    async def close(self) -> None:
        await self.tools.close()

    async def route(
        self,
        context: list[str],
        round_number: int,
        previous_agent: Optional[str] = None,
    ) -> str:
        available = ["answer"]
        if previous_agent != "think":
            available.append("think")
        if self.tools.tools and round_number <= self.max_rounds - 2:
            available.append("action")
        tool_descriptions = self.tools.descriptions() or "无"
        instruction = build_prompt(
            """
            当前轮次：{round_number}
            可选步骤：{available}

            可用工具：
            {tools}

            路由规则：
            - 查询实际观测值、记录数、统计、比较、趋势、断面或站点数据：选择 action，使用 database_query。
            - 查询内部文档、制度、报告、说明或知识依据：选择 action，使用 rag_search。
            - 询问服务器当前时间：选择 action，使用 CurrentServerTime。
            - 概念解释、普通对话或上下文信息已经足够：选择 answer。
            - 同时需要文档依据和实际数据：先调用其中一个工具，后续再调用另一个。

            {route_prompt}
            """,
            round_number=round_number,
            available=", ".join(available),
            tools=tool_descriptions,
            route_prompt=self.ROUTE_PROMPT,
        )
        prompt = build_prompt(instruction, context=context)
        response = await self.llm.invoke(
            [{"role": "user", "content": prompt}], temperature=0
        )
        try:
            choice = json.loads(response.strip()).get("agent")
        except (json.JSONDecodeError, AttributeError):
            match = re.search(r"\b(think|action|answer)\b", response, re.I)
            choice = match.group(1).lower() if match else "answer"
        return choice if choice in available else "answer"

    async def run_stream(
        self,
        question: str,
        history: list[ChatMessage],
    ) -> AsyncIterator[ReActEvent]:
        limit = get_settings().conversation.max_history_messages
        context = [f"{item.role}: {item.content}" for item in history[-limit:]]
        context.append(f"user: {question}")
        preferred_tool = self.tools.match(question)
        previous_agent: Optional[str] = None
        for round_number in range(1, self.max_rounds + 1):
            action_has_room = round_number <= self.max_rounds - 2
            if previous_agent == "action":
                next_agent = "think"
            elif self.force_answer and round_number == self.max_rounds:
                next_agent = "answer"
            elif preferred_tool and action_has_room:
                next_agent = "action"
            else:
                next_agent = await self.route(context, round_number, previous_agent)
            if next_agent == "think":
                parts: list[str] = []
                async for token in self.think_agent.run(context):
                    parts.append(token)
                    yield ReActEvent("think", token, round_number)
                summary = "".join(parts).strip()
                if summary:
                    context.append(f"assistant: 阶段性分析：{summary}")
                yield ReActEvent("think_finished", summary, round_number)
                previous_agent = "think"
                continue
            if next_agent == "action":
                yield ReActEvent("action_started", round_number=round_number)
                forced_tool = preferred_tool
                result = await self.action_agent.run(context, forced_tool)
                preferred_tool = None
                yield ReActEvent(
                    "action_finished",
                    result.observation,
                    round_number,
                    result.tool_name,
                    result.success,
                )
                context.append(
                    f"{result.tool_name or '工具'}返回：{result.observation}"
                )
                previous_agent = "action"
                continue
            produced = False
            async for token in self.answer_agent.run(context):
                produced = True
                yield ReActEvent("answer", token, round_number)
            if not produced:
                raise RuntimeError("AnswerAgent 未生成有效回答")
            return
        raise RuntimeError("ReAct 达到最大轮数但未生成回答")


def build_react_agent() -> CoreAgent:
    return CoreAgent(HelloAgentsLLM(), build_tool_executor())
