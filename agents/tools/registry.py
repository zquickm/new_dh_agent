from __future__ import annotations

from agents.tools.current_server_time import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    get_current_server_time,
    matches_current_server_time,
)
from agents.tools.executor import ToolExecutor


def build_tool_executor() -> ToolExecutor:
    executor = ToolExecutor()
    executor.register(
        TOOL_NAME,
        TOOL_DESCRIPTION,
        get_current_server_time,
        matcher=matches_current_server_time,
    )
    return executor
