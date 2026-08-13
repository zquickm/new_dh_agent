from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

from agents.events import ReActEvent
from agents.llm_client import HelloAgentsLLM
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
        "不展示详细思维过程；不直接给出最终答案。"
    )

    def __init__(self, llm: HelloAgentsLLM) -> None:
        self.llm = llm

    async def run(self, context: list[str]) -> AsyncIterator[str]:
        async for token in self.llm.stream(
            [{"role": "user", "content": build_prompt(context, self.PROMPT)}]
        ):
            yield token


class AnswerAgent:
    PROMPT = (
        "请根据上下文和思考概括直接回答用户的问题；"
        "不要输出内部思考过程。"
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
        "请快速选择下一步：think 表示分析梳理，"
        "answer 表示最终回答。只输出 JSON，例如 {\"agent\": \"think\"}。"
    )

    def __init__(self, llm: HelloAgentsLLM) -> None:
        settings = get_settings()
        self.llm = llm
        self.max_rounds = settings.react.max_rounds
        self.enable_think = settings.react.enable_think
        self.force_answer = settings.react.force_answer_on_last_round
        self.think_agent = ThinkAgent(llm)
        self.answer_agent = AnswerAgent(llm)

    @property
    def model_name(self) -> str:
        return self.llm.model

    async def route(self, context: list[str], round_number: int) -> str:
        available = ["answer"]
        if self.enable_think:
            available.append("think")
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
            match = re.search(r"\b(think|answer)\b", response, re.I)
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
            produced = False
            async for token in self.answer_agent.run(context):
                produced = True
                yield ReActEvent("answer", token, round_number)
            if not produced:
                raise RuntimeError("AnswerAgent 未生成有效回答")
            return
        raise RuntimeError("ReAct 达到最大轮数但未生成回答")


def build_react_agent() -> CoreAgent:
    return CoreAgent(HelloAgentsLLM())
