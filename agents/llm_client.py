from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_openai import ChatOpenAI

from config.settings import get_settings


class LLMInvocationError(RuntimeError):
    pass


class DSMLParseError(ValueError):
    pass


_DSML_PREFIX = r"[|｜]DSML[|｜]"
_DSML_BLOCK_RE = re.compile(
    rf"<(?P<prefix>{_DSML_PREFIX})(?P<kind>tool_calls|function_calls)\s*>"
    rf"(?P<body>.*?)"
    rf"</(?P=prefix)(?P=kind)\s*>",
    re.DOTALL,
)
_DSML_INVOKE_RE = re.compile(
    rf"<{_DSML_PREFIX}invoke(?P<attrs>[^>]*)>"
    rf"(?P<body>.*?)"
    rf"</{_DSML_PREFIX}invoke\s*>",
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    rf"<{_DSML_PREFIX}parameter(?P<attrs>[^>]*)>"
    rf"(?P<value>.*?)"
    rf"</{_DSML_PREFIX}parameter\s*>",
    re.DOTALL,
)
_DSML_ATTRIBUTE_RE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z0-9_-]*)\s*=\s*"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)
_DSML_MARKER_RE = re.compile(rf"<{_DSML_PREFIX}")


@dataclass(frozen=True)
class _ParsedDSML:
    content: str
    tool_calls: list[dict[str, Any]]


def _attributes(source: str) -> dict[str, str]:
    attributes = {
        match.group("name"): match.group("value")
        for match in _DSML_ATTRIBUTE_RE.finditer(source)
    }
    residue = _DSML_ATTRIBUTE_RE.sub("", source).strip()
    if residue:
        raise DSMLParseError("DSML 标签属性格式无效")
    return attributes


def _parse_dsml_parameters(body: str) -> dict[str, Any]:
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            arguments = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DSMLParseError("DSML 工具参数不是有效 JSON") from exc
        if not isinstance(arguments, dict):
            raise DSMLParseError("DSML 工具参数必须是 JSON 对象")
        return arguments

    arguments: dict[str, Any] = {}
    matches = list(_DSML_PARAMETER_RE.finditer(body))
    if _DSML_PARAMETER_RE.sub("", body).strip():
        raise DSMLParseError("DSML 工具参数结构不完整")
    for match in matches:
        attributes = _attributes(match.group("attrs"))
        name = attributes.get("name", "").strip()
        string_flag = attributes.get("string", "").strip().lower()
        if not name or string_flag not in {"true", "false"}:
            raise DSMLParseError("DSML parameter 缺少有效的 name 或 string 属性")
        if name in arguments:
            raise DSMLParseError(f"DSML 工具参数重复：{name}")
        raw_value = match.group("value").strip()
        if string_flag == "true":
            arguments[name] = raw_value
            continue
        try:
            arguments[name] = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise DSMLParseError(f"DSML 工具参数 {name} 不是有效 JSON") from exc
    return arguments


def _parse_dsml(content: str, tools: list[dict[str, Any]]) -> _ParsedDSML | None:
    blocks = list(_DSML_BLOCK_RE.finditer(content))
    if not blocks:
        if _DSML_MARKER_RE.search(content):
            raise DSMLParseError("DSML 工具调用结构不完整")
        return None

    allowed_names = {
        str(tool.get("function", {}).get("name") or "")
        for tool in tools
        if isinstance(tool, dict)
    }
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        body = block.group("body")
        invokes = list(_DSML_INVOKE_RE.finditer(body))
        if not invokes or _DSML_INVOKE_RE.sub("", body).strip():
            raise DSMLParseError("DSML tool_calls 中存在无效 invoke 结构")
        for invoke in invokes:
            attributes = _attributes(invoke.group("attrs"))
            name = attributes.get("name", "").strip()
            if not name or name not in allowed_names:
                raise DSMLParseError(f"DSML 调用了未注册工具：{name or '<empty>'}")
            tool_calls.append(
                {
                    "id": f"dsml-{uuid4().hex}",
                    "name": name,
                    "args": _parse_dsml_parameters(invoke.group("body")),
                    "type": "tool_call",
                }
            )

    content_without_dsml = _DSML_BLOCK_RE.sub("", content).strip()
    if _DSML_MARKER_RE.search(content_without_dsml):
        raise DSMLParseError("回答中还包含未解析的 DSML 内容")
    return _ParsedDSML(content=content_without_dsml, tool_calls=tool_calls)


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
            if tools and not tool_calls:
                parsed = _parse_dsml(content, tools)
                if parsed is not None:
                    return parsed.content, parsed.tool_calls
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
            buffered_content: list[str] = []
            async for chunk in client.astream(messages, temperature=temperature):
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
                    if tools:
                        buffered_content.append(content)
                    else:
                        yield content, []
            content = "".join(buffered_content)
            if tool_response is not None:
                tool_calls = list(tool_response.tool_calls or [])
                if tool_calls:
                    for part in buffered_content:
                        yield part, []
                    yield "", tool_calls
                    return
            if tools:
                parsed = _parse_dsml(content, tools)
                if parsed is not None:
                    if parsed.content:
                        yield parsed.content, []
                    yield "", parsed.tool_calls
                    return
                for part in buffered_content:
                    yield part, []
        except Exception as exc:
            raise LLMInvocationError("LLM stream failed") from exc
