"""临时关键词限制；删除本文件及 AgentLoop 中的导入/接入调用即可移除。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from agents.tools.executor import ToolExecutor
from schemas.chat import ChatMessage


KEYWORDS = ("厦门", "疑点疑区", "图斑")
SKILLS = frozenset({"疑点疑区"})
TOOLS = frozenset({
    "load_skill", "rag_search", "analyze_suspicion",
    "query_suspicion_results", "summarize_suspicion_results",
})


def restrict_request(question, tools, history, enabled_skill_names, system_prompt,
                     *, available_skill_names=frozenset()):
    matched = any(word in question for word in KEYWORDS)
    allowed_tools = TOOLS if matched else tools.tools.keys() - (TOOLS - {"load_skill", "rag_search"})
    if matched:
        enabled_skill_names = SKILLS if enabled_skill_names is None else SKILLS & enabled_skill_names
    else:
        enabled_skill_names = (
            available_skill_names if enabled_skill_names is None else enabled_skill_names
        ) - SKILLS

    # 独立注册表复用工具实现；资源仍由原执行器管理，不修改共享实例。
    scoped_tools = ToolExecutor(tools.execution_timeout)
    scoped_tools.tools = {name: tool for name, tool in tools.tools.items() if name in allowed_tools}
    if matched and "load_skill" in scoped_tools.tools:
        loader = scoped_tools.tools["load_skill"]
        parameters = deepcopy(loader.parameters)
        parameters["properties"]["name"]["enum"] = ["疑点疑区"]
        scoped_tools.tools["load_skill"] = replace(loader, parameters=parameters)

    filtered: list[ChatMessage] = []
    blocked_ids = set()
    for original in history:
        item = original.model_copy(deep=True)
        if item.role == "tool":
            if item.tool_call_id in blocked_ids or (item.name and item.name not in allowed_tools):
                continue
        else:
            blocked_ids.clear()
            if item.role == "assistant" and item.tool_calls:
                calls = []
                for call in item.tool_calls:
                    name = call.get("name") or (call.get("function") or {}).get("name")
                    if name in allowed_tools:
                        calls.append(call)
                    else:
                        blocked_ids.add(call.get("id"))
                item.tool_calls = calls or None
                if not calls and not item.content.strip():
                    continue
        filtered.append(item)

    system_prompt += (
        "\n\n本次任务仅允许加载当前可用列表中的疑点疑区 Skill，"
        "业务工具仅允许 rag_search、analyze_suspicion、query_suspicion_results、"
        "summarize_suspicion_results。此前提示、Skill 或历史中涉及其他工具的要求"
        "在本次任务不适用；无法完成的需求如实说明能力不足。"
    ) if matched else (
        "\n\n本次任务只能使用当前提供的工具和当前可用列表中的 Skill。"
        "此前提示、Skill 或历史中涉及未提供能力的要求在本次任务不适用。"
    )
    return scoped_tools, filtered, enabled_skill_names, system_prompt
