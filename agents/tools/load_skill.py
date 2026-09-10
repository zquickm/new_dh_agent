from __future__ import annotations

from agents.skills import SkillCatalog


TOOL_NAME = "load_skill"
TOOL_DESCRIPTION = (
    "按名称加载一个可用能力的完整操作说明。仅在当前上下文的能力列表表明该"
    "能力与任务相关时调用；同一能力在一次请求中无需重复加载。"
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "当前上下文中列出的能力名称。",
        }
    },
    "required": ["name"],
    "additionalProperties": False,
}


def load_skill(catalog: SkillCatalog, name: str) -> dict[str, str]:
    skill = catalog.load(name)
    return {
        "name": skill.name,
        "description": skill.description,
        "instructions": skill.instructions,
    }
