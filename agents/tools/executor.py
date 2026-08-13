from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional


ToolFunction = Callable[..., Any]
ToolMatcher = Callable[[str], bool]


@dataclass(frozen=True)
class RegisteredTool:
    description: str
    function: ToolFunction
    parameters: dict[str, Any]
    matcher: Optional[ToolMatcher] = None


class ToolExecutor:
    """Register and execute tools without depending on concrete implementations."""

    def __init__(self, execution_timeout: float = 300.0) -> None:
        self.tools: dict[str, RegisteredTool] = {}
        self.execution_timeout = execution_timeout
        self._closers: list[Callable[..., Any]] = []

    def register(
        self,
        name: str,
        description: str,
        function: ToolFunction,
        parameters: Optional[dict[str, Any]] = None,
        *,
        matcher: Optional[ToolMatcher] = None,
    ) -> None:
        self.tools[name] = RegisteredTool(
            description=description,
            function=function,
            parameters=parameters
            or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            matcher=matcher,
        )

    def register_closer(self, closer: Callable[..., Any]) -> None:
        self._closers.append(closer)

    def get(self, name: str) -> Optional[RegisteredTool]:
        return self.tools.get(name)

    def descriptions(self) -> str:
        return json.dumps(self.definitions(), ensure_ascii=False, indent=2)

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for name, tool in self.tools.items()
        ]

    def match(self, query: str) -> Optional[str]:
        for name, tool in self.tools.items():
            if tool.matcher is not None and tool.matcher(query):
                return name
        return None

    async def execute(self, name: str, argument: Any) -> str:
        tool = self.get(name)
        if tool is None:
            raise LookupError(f"Unknown tool: {name}")

        if isinstance(argument, str) and argument.lstrip().startswith("{"):
            try:
                parsed = json.loads(argument)
                if isinstance(parsed, dict):
                    argument = parsed
            except json.JSONDecodeError:
                pass

        args: tuple[Any, ...] = () if isinstance(argument, dict) else (argument,)
        kwargs = argument if isinstance(argument, dict) else {}

        async def invoke() -> Any:
            if inspect.iscoroutinefunction(tool.function):
                return await tool.function(*args, **kwargs)
            result = await asyncio.to_thread(tool.function, *args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result

        result = await asyncio.wait_for(invoke(), timeout=self.execution_timeout)
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    async def close(self) -> None:
        for closer in reversed(self._closers):
            result = closer()
            if inspect.isawaitable(result):
                await result
