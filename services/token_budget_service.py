from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tokenizers import Tokenizer


TOKENIZER_NAME = "deepseek-v4-official"
_TRUNCATED_TOOL_SUFFIX = "\n[工具结果因上下文长度限制已截断]"


class ContextBudgetExceeded(ValueError):
    """Raised when protected model input cannot fit the configured budget."""


@dataclass(frozen=True)
class ContextBudgetResult:
    messages: list[dict[str, Any]]
    estimated_input_tokens: int
    history_messages_included: int
    history_messages_dropped: int
    history_group_sizes: tuple[int, ...]
    tool_results_truncated: int = 0


class DeepSeekV4TokenCounter:
    """Count locally with DeepSeek's published V4 BPE tokenizer."""

    def __init__(self, tokenizer_file: Path) -> None:
        if not tokenizer_file.is_file():
            raise FileNotFoundError(f"DeepSeek V4 tokenizer not found: {tokenizer_file}")
        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))

    def count_text(self, value: str) -> int:
        return len(self._tokenizer.encode(value, add_special_tokens=False).ids)

    def count_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        rendered = self._render_messages(messages)
        if tools:
            # Chat Completions sends tools outside messages. The upstream wrapper is
            # implementation-specific, so count stable compact JSON and retain a
            # separate safety margin at the budget layer.
            rendered += "\n<｜tools▁begin｜>" + json.dumps(
                tools, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ) + "<｜tools▁end｜>"
        return self.count_text(rendered)

    @staticmethod
    def _content(message: dict[str, Any]) -> str:
        value = message.get("content")
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def _render_messages(self, messages: list[dict[str, Any]]) -> str:
        system = "\n\n".join(
            self._content(message)
            for message in messages
            if message.get("role") == "system"
        )
        parts = ["<｜begin▁of▁sentence｜>", system]
        in_tool_outputs = False
        first_tool_output = True
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            content = self._content(message)
            if role == "user":
                if in_tool_outputs:
                    parts.append("<｜tool▁outputs▁end｜>")
                    in_tool_outputs = False
                parts.extend(("<｜User｜>", content))
            elif role == "assistant":
                if in_tool_outputs:
                    parts.append("<｜tool▁outputs▁end｜>")
                    in_tool_outputs = False
                parts.append("<｜Assistant｜>")
                calls = message.get("tool_calls") or []
                if calls:
                    parts.append("<｜tool▁calls▁begin｜>")
                    for call in calls:
                        parts.extend(
                            (
                                "<｜tool▁call▁begin｜>function<｜tool▁sep｜>",
                                str(call.get("name") or ""),
                                "\n```json\n",
                                json.dumps(
                                    call.get("args") or {},
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                                "\n```<｜tool▁call▁end｜>",
                            )
                        )
                    parts.append("<｜tool▁calls▁end｜>")
                if content:
                    parts.append(content)
                parts.append("<｜end▁of▁sentence｜>")
                first_tool_output = True
            elif role == "tool":
                if not in_tool_outputs:
                    parts.append("<｜tool▁outputs▁begin｜>")
                    in_tool_outputs = True
                elif not first_tool_output:
                    parts.append("\n")
                parts.extend(
                    ("<｜tool▁output▁begin｜>", content, "<｜tool▁output▁end｜>")
                )
                first_tool_output = False
        if in_tool_outputs:
            parts.append("<｜tool▁outputs▁end｜>")
        parts.append("<｜Assistant｜>")
        return "".join(parts)


class TokenBudgetService:
    def __init__(self, counter: DeepSeekV4TokenCounter, input_budget_tokens: int):
        self.counter = counter
        self.input_budget_tokens = input_budget_tokens

    def fit(
        self,
        messages: list[dict[str, Any]],
        *,
        history_group_sizes: tuple[int, ...],
        original_history_count: int,
        tools: list[dict[str, Any]] | None,
    ) -> ContextBudgetResult:
        fitted = [dict(message) for message in messages]
        groups = list(history_group_sizes)
        while groups and self.counter.count_request(fitted, tools) > self.input_budget_tokens:
            del fitted[1 : 1 + groups.pop(0)]

        truncated = 0
        while self.counter.count_request(fitted, tools) > self.input_budget_tokens:
            tool_index = next(
                (
                    index
                    for index, message in enumerate(fitted)
                    if message.get("role") == "tool"
                    and not str(message.get("content") or "").endswith(_TRUNCATED_TOOL_SUFFIX)
                ),
                None,
            )
            if tool_index is None:
                raise ContextBudgetExceeded(
                    "系统提示词、当前问题和工具定义超过上下文输入预算"
                )
            fitted[tool_index]["content"] = self._largest_fitting_tool_content(
                fitted, tool_index, tools
            )
            truncated += 1

        included = sum(groups)
        return ContextBudgetResult(
            messages=fitted,
            estimated_input_tokens=self.counter.count_request(fitted, tools),
            history_messages_included=included,
            history_messages_dropped=max(0, original_history_count - included),
            history_group_sizes=tuple(groups),
            tool_results_truncated=truncated,
        )

    def _largest_fitting_tool_content(
        self,
        messages: list[dict[str, Any]],
        tool_index: int,
        tools: list[dict[str, Any]] | None,
    ) -> str:
        original = str(messages[tool_index].get("content") or "")
        low, high = 0, len(original)
        best = _TRUNCATED_TOOL_SUFFIX
        while low <= high:
            middle = (low + high) // 2
            candidate = original[:middle] + _TRUNCATED_TOOL_SUFFIX
            messages[tool_index]["content"] = candidate
            if self.counter.count_request(messages, tools) <= self.input_budget_tokens:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        messages[tool_index]["content"] = best
        return best
