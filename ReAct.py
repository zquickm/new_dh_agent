import json
import re
from typing import Dict, Iterator, List, Optional

from llm_client import HelloAgentsLLM
from tools import ToolExecutor, search


def build_prompt(context: List[str], instruction: str) -> str:
    """把上下文和当前 Agent 的任务拼成一个 Prompt。"""
    context_text = "\n".join(context) or "无"
    return f"""
当前上下文：
----------------
{context_text}
----------------

{instruction}
""".strip()


class CoreAgent:
    """负责路由、统计轮数，并组织其他三个 Agent。"""

    ROUTE_PROMPT = """
请快速选择下一步由哪个 Agent 处理：
- think：需要分析、梳理问题或概括当前进展。
- action：需要调用工具获取信息。
- answer：已有足够信息，可以最终回答。

只输出一个 agent 字段，不要解释。例如：
{"agent": "think"}
""".strip()

    def __init__(
        self,
        llm_client: HelloAgentsLLM,
        tool_executor: ToolExecutor,
        max_rounds: int = 5,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self.llm_client = llm_client
        self.max_rounds = max_rounds
        self.think_agent = ThinkAgent(llm_client)
        self.action_agent = ActionAgent(llm_client, tool_executor)
        self.answer_agent = AnswerAgent(llm_client)

    def run(self, question: str) -> str:
        """同步使用：在终端展示 Think/Answer 流，并返回最终答案。"""
        answer_parts: List[str] = []
        current_event: Optional[str] = None

        for event in self.run_stream(question):
            event_name = event["event"]
            content = event["content"]
            if event_name != current_event:
                if current_event is not None:
                    print()
                label = "🤔 思考概括: " if event_name == "think" else "🎉 最终答案: "
                print(label, end="", flush=True)
                current_event = event_name
            print(content, end="", flush=True)
            if event_name == "answer":
                answer_parts.append(content)

        if current_event is not None:
            print()
        return "".join(answer_parts)

    def run_stream(self, question: str) -> Iterator[Dict[str, str]]:
        """
        流式使用：只向外部输出 think 和 answer 事件。

        上下文只在本次 run 内存在，暂不处理跨轮对话。
        """
        context = [f"user: {question}"]
        round_number = 1

        while True:
            next_agent = self.route(context, round_number)

            if next_agent == "answer":
                answer_parts: List[str] = []
                for token in self.answer_agent.run(context):
                    answer_parts.append(token)
                    yield {"event": "answer", "content": token}
                context.append(f"assistant: {''.join(answer_parts)}")
                return

            if next_agent == "think":
                summary_parts: List[str] = []
                for token in self.think_agent.run(context):
                    summary_parts.append(token)
                    yield {"event": "think", "content": token}
                summary = "".join(summary_parts).strip()
                if summary:
                    context.append(f"assistant: 阶段性分析：{summary}")

            elif next_agent == "action":
                tool_result = self.action_agent.run(context)
                if tool_result:
                    context.append(tool_result)

            # ThinkAgent 和 ActionAgent 完成后一定回到 CoreAgent。
            round_number += 1

    def route(self, context: List[str], round_number: int) -> str:
        # 最后一轮不再请求模型路由，直接交给 AnswerAgent。
        if round_number >= self.max_rounds:
            return "answer"

        prompt = build_prompt(
            context,
            f"当前是第 {round_number} 轮，最多 {self.max_rounds} 轮。\n\n{self.ROUTE_PROMPT}",
        )
        response = self.llm_client.think(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        return self._parse_route(response)

    @staticmethod
    def _parse_route(response: Optional[str]) -> str:
        if not response:
            return "answer"

        text = response.strip()
        try:
            data = json.loads(text)
            agent_name = data.get("agent")
        except (json.JSONDecodeError, AttributeError):
            match = re.search(
                r'["\']?agent["\']?\s*:\s*["\']?(think|action|answer)',
                text,
                re.IGNORECASE,
            )
            agent_name = match.group(1) if match else None

        agent_name = agent_name.lower() if isinstance(agent_name, str) else None
        return agent_name if agent_name in {"think", "action", "answer"} else "answer"


class ThinkAgent:
    """生成可展示的简短思考概括，并隐式指导后续 Agent。"""

    PROMPT = """
请概括当前问题的处理进展，并说明下一步需要什么信息。

要求：
1. 简短、清楚，可以直接展示给用户。
2. 只输出推理概括，不展示详细思维过程。
3. 不调用工具，不直接给出最终答案。
""".strip()

    def __init__(self, llm_client: HelloAgentsLLM) -> None:
        self.llm_client = llm_client

    def run(self, context: List[str]) -> Iterator[str]:
        prompt = build_prompt(context, self.PROMPT)
        yield from self.llm_client.think_stream(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )


class ActionAgent:
    """选择并调用一个工具，只把工具返回值加入上下文。"""

    PROMPT = """
请根据当前上下文选择一个工具。

可用工具：
{tools}

只能输出：工具名称[工具参数]
例如：Search[华为最新发布的手机]
不要解释，不要回答用户。
""".strip()

    def __init__(
        self,
        llm_client: HelloAgentsLLM,
        tool_executor: ToolExecutor,
    ) -> None:
        self.llm_client = llm_client
        self.tool_executor = tool_executor

    def run(self, context: List[str]) -> Optional[str]:
        prompt = build_prompt(
            context,
            self.PROMPT.format(tools=self.tool_executor.getAvailableTools()),
        )
        action_text = self.llm_client.think(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        tool_name, tool_input = self._parse_action(action_text or "")

        if not tool_name:
            return None

        tool_function = self.tool_executor.getTool(tool_name)
        if tool_function is None:
            return f"{tool_name}返回: 未找到该工具。"

        try:
            observation = tool_function(tool_input)
        except Exception as exc:
            observation = f"工具执行失败：{exc}"
        return f"{tool_name}返回: {observation}"

    @staticmethod
    def _parse_action(action_text: str):
        match = re.search(r"([\w.-]+)\[(.*)\]", action_text.strip(), re.DOTALL)
        if not match:
            return None, None
        return match.group(1), match.group(2).strip()


class AnswerAgent:
    """根据上下文生成最终答案，完成本轮问答。"""

    PROMPT = """
请根据上下文，直接回答用户的问题。

要求：
1. 合理使用思考概括和工具返回的信息。
2. 不输出内部思考和调用工具的过程。
""".strip()

    def __init__(self, llm_client: HelloAgentsLLM) -> None:
        self.llm_client = llm_client

    def run(self, context: List[str]) -> Iterator[str]:
        prompt = build_prompt(context, self.PROMPT)
        yield from self.llm_client.think_stream(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )



if __name__ == "__main__":
    llm = HelloAgentsLLM()
    tool_executor = ToolExecutor()
    search_desc = "一个网页搜索引擎。需要查询时事或外部事实时使用。"
    tool_executor.registerTool("Search", search_desc, search)
    agent = CoreAgent(llm_client=llm, tool_executor=tool_executor)
    agent.run("华为最新的手机是哪一款？它的主要卖点是什么？")
