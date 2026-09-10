from __future__ import annotations

from typing import Any, Optional, Protocol


class ToolAuditRecorder(Protocol):
    async def started(
        self,
        *,
        tool_call_id: str,
        round_number: int,
        sequence_no: int,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None: ...

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
    ) -> None: ...
