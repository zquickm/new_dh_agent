from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional


ToolFunction = Callable[[str], str]


@dataclass(frozen=True)
class RegisteredTool:
    description: str
    function: ToolFunction


class ToolExecutor:
    """Generic ReAct tool registry, independent of any concrete tool provider."""

    def __init__(self) -> None:
        self.tools: dict[str, RegisteredTool] = {}

    def register(self, name: str, description: str, function: ToolFunction) -> None:
        self.tools[name] = RegisteredTool(description, function)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self.tools.get(name)

    def descriptions(self) -> str:
        return "\n".join(
            f"- {name}: {tool.description}" for name, tool in self.tools.items()
        )

    async def execute(self, name: str, argument: str) -> str:
        tool = self.get(name)
        if tool is None:
            raise LookupError(f"Unknown tool: {name}")
        return await asyncio.to_thread(tool.function, argument)


def build_tool_executor() -> ToolExecutor:
    """Build the registry; concrete tools can be registered here later."""

    return ToolExecutor()
