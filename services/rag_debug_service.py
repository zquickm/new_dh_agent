"""Administrator-only RAG diagnostics in the existing run event JSON column."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4


logger = logging.getLogger(__name__)
DEBUG_EVENT_TYPES = ("rag_debug", "rag_debug_part")
PART_CHARS = 24000
_recorder: ContextVar[Optional[Callable[[str, dict], Awaitable[None]]]] = ContextVar("rag_debug", default=None)
_SECRET_KEYS = {"authorization", "cookie", "setcookie", "password", "passwd", "token", "apitoken", "apikey", "accesstoken", "refreshtoken", "secret", "clientsecret", "xdhinternaltoken"}


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if str(key).lower().replace("-", "").replace("_", "") in
                _SECRET_KEYS else sanitize(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


@contextmanager
def rag_debug_scope(callback):
    token = _recorder.set(callback)
    try:
        yield
    finally:
        _recorder.reset(token)


async def record_rag_debug(stage: str, **data) -> None:
    callback = _recorder.get()
    if callback is not None:
        await callback(stage, data)


class RagDebugWriter:
    """Write parts first, then a manifest. An interrupted write has no complete manifest."""

    def __init__(self, repository, run_id: str, user_id: str):
        self.repository, self.run_id, self.user_id = repository, run_id, user_id
        self.failed_captures = 0

    async def record(self, stage: str, data: dict) -> None:
        capture_id = str(uuid4())
        try:
            clean = sanitize(data)
            serialized = json.dumps(clean, ensure_ascii=False, allow_nan=False, sort_keys=True, default=str)
            raw = serialized.encode("utf-8")
            metadata = {"schema_version": 1, "capture_id": capture_id, "stage": stage,
                        "tool_call_id": clean.get("tool_call_id"), "round_number": clean.get("round_number"),
                        "request_id": clean.get("request_id"), "attempt": clean.get("attempt"),
                        "knowledge_id": clean.get("knowledge_id"),
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                        "previous_capture_failures": self.failed_captures}
            # Responses and large stages are always loaded explicitly by administrators.
            split = stage == "response" or len(serialized) > PART_CHARS
            parts = [serialized[i:i + PART_CHARS] for i in range(0, len(serialized), PART_CHARS)] if split else []
            for index, part in enumerate(parts):
                await self._append("rag_debug_part", {"capture_id": capture_id, "part_index": index,
                                                       "part_count": len(parts), "text": part})
            await self._append("rag_debug", {**metadata, "part_count": len(parts), "complete": True,
                                              "data": None if split else clean})
        except asyncio.CancelledError:
            self.failed_captures += 1
            try:
                await self._failure_marker(capture_id, stage, data, "采集被取消，该阶段不完整")
            except Exception:
                logger.error("rag_debug_cancel_marker_unavailable run_id=%s", self.run_id)
            raise
        except Exception:
            # Diagnostics must not change retrieval results or abort the user's answer.
            self.failed_captures += 1
            logger.exception("rag_debug_write_failed run_id=%s capture_id=%s stage=%s", self.run_id, capture_id, stage)
            try:
                await self._failure_marker(capture_id, stage, data, "调试数据保存失败，该阶段不完整")
            except Exception:
                logger.error("rag_debug_failure_marker_unavailable run_id=%s", self.run_id)

    async def _failure_marker(self, capture_id, stage, data, error):
        await self._append("rag_debug", {"schema_version": 1, "capture_id": capture_id,
            "stage": stage, "tool_call_id": data.get("tool_call_id"), "round_number": data.get("round_number"),
            "request_id": data.get("request_id"), "attempt": data.get("attempt"), "knowledge_id": data.get("knowledge_id"),
            "complete": False, "previous_capture_failures": self.failed_captures,
            "error": error, "data": None, "part_count": 0})

    async def _append(self, event_type, data):
        return await self.repository.append_run_event(self.run_id, event_type, data, user_id=self.user_id)


def restore_capture(manifest: dict, parts: list[dict]) -> dict:
    count = manifest.get("part_count", 0)
    if not manifest.get("complete"):
        return {**manifest, "complete": False}
    if count:
        ordered = sorted(parts, key=lambda item: item["part_index"])
        if len(ordered) != count or [item["part_index"] for item in ordered] != list(range(count)):
            return {**manifest, "complete": False, "error": "分片缺失，数据不完整"}
        serialized = "".join(item["text"] for item in ordered)
    else:
        serialized = json.dumps(manifest.get("data"), ensure_ascii=False, allow_nan=False, sort_keys=True, default=str)
    raw = serialized.encode("utf-8")
    if len(raw) != manifest.get("bytes") or hashlib.sha256(raw).hexdigest() != manifest.get("sha256"):
        return {**manifest, "complete": False, "error": "校验失败，数据不完整", "data": None}
    return {**manifest, "data": json.loads(serialized)}
