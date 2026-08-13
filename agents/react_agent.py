from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Optional

from agents.events import ActionResult, ReActEvent
from agents.llm_client import HelloAgentsLLM
from agents.tools.executor import ToolExecutor
from agents.tools.registry import build_tool_executor
from config.settings import get_settings
from schemas.chat import ChatMessage


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

    async def run(
        self,
        context: list[str],
        preferred_tool: Optional[str] = None,
    ) -> ActionResult:
        if preferred_tool:
            try:
                observation = await self.tools.execute(preferred_tool, "")
                return ActionResult(preferred_tool, "", observation, True)
            except Exception:
                return ActionResult(preferred_tool, "", "工具执行失败", False)
        prompt = build_prompt(
            context,
            "请根据完整上下文选择最适合当前步骤的一个工具。\n可用工具：\n"
            f"{self.tools.descriptions()}\n"
            "必须严格按照工具的 parameters 生成 JSON 参数对象，并完整保留用户给出的"
            "时间、断面、站点、指标和过滤条件。只能输出 工具名称[JSON参数对象]，"
            "不要输出 Markdown、解释或最终答案。",
        )
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
        except Exception:
            return ActionResult(name, argument, "工具执行失败", False)


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
        tool_descriptions = self.tools.descriptions() or "无"
        prompt = build_prompt(
            context,
            f"当前第 {round_number} 轮，可选：{', '.join(available)}。\n"
            f"可用工具：\n{tool_descriptions}\n"
            "路由规则：查询实际观测值、记录数、统计、比较、趋势、断面或站点数据时，"
            "选择 action 并使用 database_query；查询内部文档、制度、报告、说明或知识依据时，"
            "选择 action 并使用 rag_search；询问服务器当前时间时使用 CurrentServerTime。"
            "概念解释、普通对话或上下文信息已经足够时选择 answer。"
            "同时需要文档依据和实际数据时，先调用其中一个工具，下一轮再调用另一个；"
            "不得在存在合适工具时直接回答无法查询。\n"
            f"{self.ROUTE_PROMPT}",
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
        preferred_tool = (
            self.tools.match(question) if self.enable_tools else None
        )
        answer_after_tool = False
        for round_number in range(1, self.max_rounds + 1):
            next_agent = (
                "answer"
                if answer_after_tool
                or (self.force_answer and round_number == self.max_rounds)
                else "action"
                if preferred_tool
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
                answer_after_tool = forced_tool is not None
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
