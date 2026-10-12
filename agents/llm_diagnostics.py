"""Bounded Chat Completions diagnostics before and after LangChain conversion."""
from __future__ import annotations

import json
import logging
from collections import Counter
from contextvars import ContextVar
from typing import Any

from langchain_openai import ChatOpenAI as BaseChatOpenAI

from tool_backends.rag_models import unique_file_sources


logger = logging.getLogger(__name__)
_active: ContextVar[Any] = ContextVar("llm_diagnostics", default=None)


class StreamDiagnostics:
    def __init__(self, max_chars: int, max_events: int, run_id: str = "-", round_number: int = 0):
        self.run_id, self.round_number = run_id, round_number
        self.max_chars = max_chars
        self.max_events = max_events
        self.chars: Counter = Counter()
        self.events: Counter = Counter()
        self.counts: Counter = Counter()
        self.finish_reasons: Counter = Counter()
        self.response_id = None
        self.model = None

    def record(self, stage: str, payload: Any, *, tools: bool = False):
        lane = stage + ("_tools" if tools else "_text")
        self.events[lane] += 1
        remaining = self.max_chars - self.chars[lane]
        if remaining <= 0 or self.events[lane] > self.max_events:
            self.counts[lane + "_omitted"] += 1
            return
        # Only response content is passed here; request headers and credentials are never logged.
        text = json.dumps(payload, ensure_ascii=False, default=str)
        truncated = len(text) > remaining
        text = text[:remaining]
        self.chars[lane] += len(text)
        logger.info(
            "llm_diagnostic run_id=%s round=%s stage=%s seq=%d truncated=%s data=%s",
            self.run_id, self.round_number, lane, self.events[lane], truncated, text,
        )

    def upstream(self, chunk: dict):
        self.counts["upstream_chunks"] += 1
        self.response_id = chunk.get("id") or self.response_id
        self.model = chunk.get("model") or self.model
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            calls = delta.get("tool_calls") or []
            self.counts["upstream_tool_deltas"] += len(calls)
            self.counts["upstream_content_chars"] += len(delta.get("content") or "")
            reason = choice.get("finish_reason")
            if reason:
                self.finish_reasons[str(reason)] += 1
            if calls or delta.get("function_call"):
                self.record("upstream", {"index": choice.get("index"), "delta": delta}, tools=True)
            elif delta:
                self.record("upstream", {"index": choice.get("index"), "delta": delta})
        # End reasons and usage are retained even when the content budget is exhausted.
        if chunk.get("usage"):
            logger.info("llm_usage run_id=%s round=%s usage=%s", self.run_id,
                        self.round_number, chunk["usage"])

    def parsed(self, chunk: Any):
        self.counts["parsed_chunks"] += 1
        payload = {
            "content": getattr(chunk, "content", ""),
            "tool_call_chunks": getattr(chunk, "tool_call_chunks", None),
            "tool_calls": getattr(chunk, "tool_calls", None),
            "invalid_tool_calls": getattr(chunk, "invalid_tool_calls", None),
            "finish_reason": (getattr(chunk, "response_metadata", None) or {}).get("finish_reason"),
        }
        self.counts["parsed_tool_deltas"] += len(payload["tool_call_chunks"] or [])
        self.counts["parsed_invalid_calls"] += len(payload["invalid_tool_calls"] or [])
        self.record("langchain", payload, tools=bool(
            payload["tool_call_chunks"] or payload["tool_calls"] or payload["invalid_tool_calls"]
        ))

    def finish(self, tool_response: Any, status: str):
        calls = list(getattr(tool_response, "tool_calls", None) or [])
        invalid = list(getattr(tool_response, "invalid_tool_calls", None) or [])
        self.record("application", {"tool_calls": calls, "invalid_tool_calls": invalid}, tools=True)
        logger.info(
            "llm_stream_summary run_id=%s round=%s status=%s counts=%s "
            "finish_reasons=%s valid_calls=%d invalid_calls=%d response_id=%s model=%s",
            self.run_id, self.round_number, status, dict(self.counts),
            dict(self.finish_reasons), len(calls), len(invalid), self.response_id, self.model,
        )


class DiagnosticChatOpenAI(BaseChatOpenAI):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        # SDK-decoded upstream JSON, before LangChain parses/normalizes tool calls.
        diagnostic = _active.get()
        if diagnostic is not None:
            diagnostic.upstream(chunk)
        return super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )


def observation_log_summary(observation: str) -> dict:
    """Retain query/status metadata without logging retrieved documents or data rows."""
    try:
        value = json.loads(observation)
    except (ValueError, TypeError):
        return {"chars": len(observation)}
    if not isinstance(value, dict):
        return {"chars": len(observation)}
    summary = {key: value[key] for key in ("success", "operation", "total", "count", "truncated") if key in value}
    for key in ("query", "sql", "error"):
        if key in value:
            summary[key] = str(value[key])[:1000]
    if isinstance(value.get("rows"), list):
        summary["row_count"] = len(value["rows"])
    if isinstance(value.get("chunks"), list):
        summary["chunk_count"] = len(value["chunks"])
        summary["articles"] = [source[:200] for source in unique_file_sources(
            str(chunk.get("source", ""))
            for chunk in value["chunks"] if isinstance(chunk, dict)
        )[:20]]
    return summary
