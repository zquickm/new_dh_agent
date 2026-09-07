from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langchain_openai import ChatOpenAI

from config.settings import get_settings


class LLMInvocationError(RuntimeError):
    pass


class HelloAgentsLLM:
    def __init__(self) -> None:
        settings = get_settings().llm
        api_key = settings.api_key.get_secret_value().strip() or "not-required"
        self.model = settings.model
        self.temperature = settings.temperature
        self.client = ChatOpenAI(
            model=settings.model,
            # The OpenAI SDK requires a non-empty value even when the compatible
            # upstream does not authenticate. A configured real key still wins.
            api_key=api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            temperature=settings.temperature,
        )

    @staticmethod
    def _content(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "".join(
                str(item.get("text", "")) if isinstance(item, dict) else str(item)
                for item in value
            )
        return str(value or "")

    async def invoke(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0,
    ) -> tuple[str, list[dict[str, Any]]]:
        try:
            client = self.client.bind_tools(tools) if tools else self.client
            response = await client.ainvoke(messages, temperature=temperature)
            content = self._content(response.content)
            tool_calls = list(response.tool_calls or [])
            return content, tool_calls
        except Exception as exc:
            raise LLMInvocationError("LLM invocation failed") from exc

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0,
    ) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
        try:
            client = self.client.bind_tools(tools) if tools else self.client
            tool_response = None
            options = {} if tools else {"tool_choice": "none"}
            async for chunk in client.astream(messages, temperature=temperature, **options):
                has_tool_delta = bool(
                    getattr(chunk, "tool_call_chunks", None)
                    or getattr(chunk, "tool_calls", None)
                )
                if tools and has_tool_delta:
                    tool_response = (
                        chunk if tool_response is None else tool_response + chunk
                    )
                content = self._content(chunk.content)
                if content:
                    yield content, []
            if tool_response is not None:
                tool_calls = list(tool_response.tool_calls or [])
                if tool_calls:
                    yield "", tool_calls
        except Exception as exc:
            raise LLMInvocationError("LLM stream failed") from exc
