"""业务场景规则；不依赖具体工具或提示词组装。"""
from __future__ import annotations

from config.settings import get_settings
from schemas.chat import FrontContext


def resolve_rag_scenario(context: FrontContext | None) -> str:
    module_l2 = context.module_l2 if context else ""
    return module_l2 if module_l2 and module_l2 in get_settings().business_scenarios else "general"


def is_xiamen_query(query: str) -> bool:
    return any(word in query for word in ("厦门", "疑点疑区", "图斑"))


def resolve_query_scenario(query: str, base_scenario: str = "general") -> str:
    """调用方传入扩展后的问题或检索词，厦门匹配优先于板块场景。"""
    return "xiamen" if is_xiamen_query(query) else base_scenario
