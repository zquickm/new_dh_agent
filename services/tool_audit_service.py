from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from repositories.repository import conversation_repository


_SECRET_KEY = re.compile(
    r"(?i)(authorization|password|passwd|token|api[_-]?key|secret|cookie)"
)
_SECRET_TEXT = re.compile(
    r"(?i)(password|passwd|token|api[_-]?key|authorization|secret)"
    r"\s*[=:]\s*([^\s,;]+)"
)
_URL_PASSWORD = re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)")
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_ARGUMENT_LIMIT = 16 * 1024
_RESULT_LIMIT = 32 * 1024


def redact_text(value: str, limit: int = 2000) -> str:
    redacted = _SECRET_TEXT.sub(r"\1=***", value)
    redacted = _URL_PASSWORD.sub(r"\1***\2", redacted)
    redacted = _BEARER_TOKEN.sub("Bearer ***", redacted)
    return redacted[:limit]


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[depth-limited]"
    if isinstance(value, dict):
        return {
            str(key): (
                "***"
                if _SECRET_KEY.search(str(key))
                else _sanitize(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return redact_text(value, limit=4000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value), limit=4000)


def _was_structurally_truncated(value: Any, *, depth: int = 0) -> bool:
    if depth >= 8:
        return True
    if isinstance(value, dict):
        return len(value) > 100 or any(
            _was_structurally_truncated(item, depth=depth + 1)
            for item in list(value.values())[:100]
        )
    if isinstance(value, (list, tuple)):
        return len(value) > 100 or any(
            _was_structurally_truncated(item, depth=depth + 1)
            for item in list(value)[:100]
        )
    if isinstance(value, str):
        return len(value) > 4000
    if value is None or isinstance(value, (bool, int, float)):
        return False
    return len(str(value)) > 4000


def sanitize_arguments(arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    structurally_truncated = _was_structurally_truncated(arguments)
    sanitized = _sanitize(arguments)
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    encoded = json.dumps(sanitized, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= _ARGUMENT_LIMIT:
        return sanitized, structurally_truncated
    return {
        "summary": "arguments exceeded audit limit",
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }, True


def _parse_result(result: str) -> Any:
    try:
        return json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return result


def _rag_summary(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return "RAG result returned"
    chunks = parsed.get("chunks") if isinstance(parsed.get("chunks"), list) else []
    return {
        "query": parsed.get("query"),
        "result_count": parsed.get("result_count", len(chunks)),
        "sources": [
            {
                key: chunk.get(key)
                for key in (
                    "source",
                    "title",
                    "document_id",
                    "chunk_id",
                    "knowledge_id",
                    "page_index",
                    "line",
                )
                if chunk.get(key) is not None
            }
            for chunk in chunks[:100]
            if isinstance(chunk, dict)
        ],
    }


def _database_summary(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return "database result returned"
    rows = parsed.get("rows") if isinstance(parsed.get("rows"), list) else []
    sql = str(parsed.get("sql") or "")
    return {
        "success": parsed.get("success"),
        "row_count": parsed.get("row_count", len(rows)),
        "columns": list(rows[0].keys())[:100] if rows and isinstance(rows[0], dict) else [],
        "truncated": parsed.get("truncated", False),
        "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest() if sql else None,
        "error": _sanitize(parsed.get("error")),
    }


def _observation_summary(parsed: Any) -> Any:
    if not isinstance(parsed, dict):
        return "observation result returned"
    return {
        "operation": parsed.get("operation"),
        "total": parsed.get("total"),
        "result_keys": sorted(str(key) for key in parsed.keys())[:100],
        "item_counts": {
            str(key): len(value)
            for key, value in parsed.items()
            if isinstance(value, list)
        },
    }


def summarize_result(tool_name: str, result: str) -> tuple[str, int, str, bool]:
    raw = result.encode("utf-8", errors="replace")
    parsed = _parse_result(result)
    if tool_name == "rag_search":
        summary = _rag_summary(parsed)
    elif tool_name == "database_query":
        summary = _database_summary(parsed)
    elif tool_name == "observation_service":
        summary = _observation_summary(parsed)
    elif tool_name == "load_skill":
        summary = {"status": "skill instructions loaded"}
    elif tool_name == "map_action":
        summary = {"status": "map action generated"}
    else:
        summary = _sanitize(parsed)
    summary = _sanitize(summary)
    text = (
        summary
        if isinstance(summary, str)
        else json.dumps(summary, ensure_ascii=False, default=str)
    )
    encoded = text.encode("utf-8")
    truncated = len(encoded) > _RESULT_LIMIT
    if truncated:
        text = encoded[:_RESULT_LIMIT].decode("utf-8", errors="ignore")
    return text, len(raw), hashlib.sha256(raw).hexdigest(), truncated


class PersistentToolAuditRecorder:
    def __init__(self, run_id: str, repository=None) -> None:
        self.run_id = run_id
        self._repository = repository or conversation_repository
        self._tool_names: dict[str, str] = {}

    async def started(
        self,
        *,
        tool_call_id: str,
        round_number: int,
        sequence_no: int,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        sanitized, truncated = sanitize_arguments(arguments)
        await self._repository.begin_tool_call(
            self.run_id,
            tool_call_id=tool_call_id,
            round_number=round_number,
            sequence_no=sequence_no,
            tool_name=tool_name,
            arguments=sanitized,
            arguments_truncated=truncated,
        )
        self._tool_names[tool_call_id] = tool_name

    async def finished(
        self,
        *,
        tool_call_id: str,
        status: str,
        result: str,
        duration_ms: int,
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        failure_stage: Optional[str] = None,
        retryable: bool = False,
    ) -> None:
        tool_name = self._tool_names.get(tool_call_id, "")
        summary, size, digest, truncated = summarize_result(tool_name, result)
        await self._repository.finish_tool_call(
            self.run_id,
            tool_call_id=tool_call_id,
            status=status,
            result_summary=summary,
            result_size_bytes=size,
            result_sha256=digest,
            result_truncated=truncated,
            duration_ms=duration_ms,
            error_code=error_code,
            error_type=error_type,
            error_message=redact_text(error_message or "") or None,
            failure_stage=failure_stage,
            retryable=retryable,
        )
        self._tool_names.pop(tool_call_id, None)
