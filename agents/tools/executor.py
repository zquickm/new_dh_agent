from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional


ToolFunction = Callable[[str], str]
ToolMatcher = Callable[[str], bool]


@dataclass(frozen=True)
class RegisteredTool:
    description: str
    function: ToolFunction
    matcher: Optional[ToolMatcher] = None


class ToolExecutor:
    """Register and execute tools without depending on concrete implementations."""

    def __init__(self) -> None:
        self.tools: dict[str, RegisteredTool] = {}

    def register(
        self,
        name: str,
        description: str,
        function: ToolFunction,
        *,
        matcher: Optional[ToolMatcher] = None,
    ) -> None:
        self.tools[name] = RegisteredTool(description, function, matcher)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self.tools.get(name)

    def descriptions(self) -> str:
        return "\n".join(
            f"- {name}: {tool.description}" for name, tool in self.tools.items()
        )

    def match(self, query: str) -> Optional[str]:
        for name, tool in self.tools.items():
            if tool.matcher is not None and tool.matcher(query):
                return name
        return None

    async def execute(self, name: str, argument: str) -> str:
        tool = self.get(name)
        if tool is None:
            raise LookupError(f"Unknown tool: {name}")
        return await asyncio.to_thread(tool.function, argument)
