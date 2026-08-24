from __future__ import annotations

from agents.tools.current_server_time import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    get_current_server_time,
    matches_current_server_time,
)
from agents.tools.database_query import (
    TOOL_DESCRIPTION as DATABASE_TOOL_DESCRIPTION,
    TOOL_NAME as DATABASE_TOOL_NAME,
    TOOL_PARAMETERS as DATABASE_TOOL_PARAMETERS,
    DatabaseQueryTool,
)
from agents.tools.executor import ToolExecutor
from agents.tools.observation_service import (
    TOOL_DESCRIPTION as OBSERVATION_TOOL_DESCRIPTION,
    TOOL_NAME as OBSERVATION_TOOL_NAME,
    TOOL_PARAMETERS as OBSERVATION_TOOL_PARAMETERS,
    ObservationServiceTool,
)
from agents.tools.rag_search import (
    TOOL_DESCRIPTION as RAG_TOOL_DESCRIPTION,
    TOOL_NAME as RAG_TOOL_NAME,
    TOOL_PARAMETERS as RAG_TOOL_PARAMETERS,
    RagSearchTool,
)
from config.settings import get_settings


def build_tool_executor() -> ToolExecutor:
    executor = ToolExecutor(get_settings().tools.execution_timeout)
    executor.register(
        TOOL_NAME,
        TOOL_DESCRIPTION,
        get_current_server_time,
        matcher=matches_current_server_time,
    )
    rag_tool = RagSearchTool()
    database_tool = DatabaseQueryTool()
    observation_tool = ObservationServiceTool()
    executor.register(
        RAG_TOOL_NAME,
        RAG_TOOL_DESCRIPTION,
        rag_tool.execute,
        RAG_TOOL_PARAMETERS,
    )
    executor.register(
        DATABASE_TOOL_NAME,
        DATABASE_TOOL_DESCRIPTION,
        database_tool.execute,
        DATABASE_TOOL_PARAMETERS,
    )
    executor.register(
        OBSERVATION_TOOL_NAME,
        OBSERVATION_TOOL_DESCRIPTION,
        observation_tool.execute,
        OBSERVATION_TOOL_PARAMETERS,
    )
    executor.register_closer(rag_tool.close)
    executor.register_closer(database_tool.close)
    executor.register_closer(observation_tool.close)
    return executor
