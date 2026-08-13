from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from agents.events import ActionResult, ReActEvent
from agents.llm_client import HelloAgentsLLM
from agents.tools import ToolExecutor, build_tool_executor
from config.settings import get_settings
from schemas.chat import ChatMessage


logger = logging.getLogger(__name__)


def build_prompt(context: list[str], instruction: str) -> str:
    context_text = "\n".join(context) or "无"
    return (
        "当前上下文：\n----------------\n"
        f"{context_text}\n"
        "----------------\n\n"
        f"{instruction}"
    )


class ThinkAgent:
    PROMPT = (
        "请概括当前问题的处理进展，并说明下一步需要什么信息。\n"
        "要求：简短、清楚，可以直接展示给用户；只输出推理概括，"
        "不展示详细思维过程；不调用工具，不直接给出最终答案。"
    )

    def __init__(self, llm: HelloAgentsLLM) -> None:
        self.llm = llm

    async def run(self, context: list[str]) -> AsyncIterator[str]:
        async for token in self.llm.stream(
            [{"role": "user", "content": build_prompt(context, self.PROMPT)}]
        ):
            yield token


class ActionAgent:
    def __init__(self, llm: HelloAgentsLLM, tools: ToolExecutor) -> None:
        self.llm = llm
        self.tools = tools

    async def run(self, context: list[str]) -> ActionResult:
        prompt = build_prompt(
            context,
            "请根据上下文选择一个工具。\n可用工具：\n"
            f"{self.tools.descriptions()}\n"
            "只能输出 工具名称[JSON参数对象]，不要解释或回答用户。\n"
            "例如：rag_search[{\"query\":\"东海赤潮形成原因\",\"top_k\":5}]\n"
            "参数中的 query 或 question 必须是脱离上下文也能理解的完整问题。",
        )
        action_text = await self.llm.invoke(
            [{"role": "user", "content": prompt}], temperature=0
        )
        match = re.fullmatch(r"([\w.-]+)\s*\[(.*)\]\s*", action_text.strip(), re.DOTALL)
        if not match:
            return ActionResult("", "", "模型未返回有效工具调用", False)
        name, raw_argument = match.group(1), match.group(2).strip()
        try:
            argument: Any = json.loads(raw_argument)
        except json.JSONDecodeError:
            return ActionResult(name, raw_argument, "工具参数不是合法 JSON 对象", False)
        if not isinstance(argument, dict):
            return ActionResult(name, raw_argument, "工具参数必须是 JSON 对象", False)
        try:
            observation = await self.tools.execute(name, argument)
            return ActionResult(name, raw_argument, observation, True)
        except Exception:
            logger.exception("Tool %s failed", name)
            return ActionResult(name, raw_argument, "工具执行失败", False)


class AnswerAgent:
    PROMPT = (
        "请根据上下文直接回答用户的问题。合理使用思考概括和工具返回的信息；"
        "不要输出内部思考和调用工具的过程。"
    )

    def __init__(self, llm: HelloAgentsLLM) -> None:
        self.llm = llm

    async def run(self, context: list[str]) -> AsyncIterator[str]:
        async for token in self.llm.stream(
            [{"role": "user", "content": build_prompt(context, self.PROMPT)}]
        ):
            yield token


class CoreAgent:
    ROUTE_PROMPT = (
        "请快速选择下一步：think 表示分析梳理，action 表示调用工具，"
        "answer 表示最终回答。只输出 JSON，例如 {\"agent\": \"think\"}。"
    )

    def __init__(self, llm: HelloAgentsLLM, tools: ToolExecutor) -> None:
        settings = get_settings()
        self.llm = llm
        self.tools = tools
        self.max_rounds = settings.react.max_rounds
        self.enable_think = settings.react.enable_think
        self.enable_tools = settings.react.enable_tools
        self.force_answer = settings.react.force_answer_on_last_round
        self.think_agent = ThinkAgent(llm)
        self.action_agent = ActionAgent(llm, tools)
        self.answer_agent = AnswerAgent(llm)

    @property
    def model_name(self) -> str:
        return self.llm.model

    async def close(self) -> None:
        await self.tools.close()

    async def route(self, context: list[str], round_number: int) -> str:
        available = ["answer"]
        if self.enable_think:
            available.append("think")
        if self.enable_tools and self.tools.tools:
            available.append("action")
        prompt = build_prompt(
            context,
            f"当前第 {round_number} 轮，可选：{', '.join(available)}。\n{self.ROUTE_PROMPT}",
        )
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
        for round_number in range(1, self.max_rounds + 1):
            next_agent = (
                "answer"
                if self.force_answer and round_number == self.max_rounds
                else await self.route(context, round_number)
            )
            if next_agent == "think":
                parts: list[str] = []
                async for token in self.think_agent.run(context):
                    parts.append(token)
                    yield ReActEvent("think", token, round_number)
                summary = "".join(parts).strip()
                if summary:
                    context.append(f"assistant: 阶段性分析：{summary}")
                yield ReActEvent("think_finished", summary, round_number)
                continue
            if next_agent == "action":
                yield ReActEvent("action_started", round_number=round_number)
                result = await self.action_agent.run(context)
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
