from __future__ import annotations

from functools import partial
from typing import Optional

from agents.skills import SkillCatalog
from agents.tools.current_server_time import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    get_current_server_time,
    matches_current_server_time,
)
from agents.tools.database_query import (
    TOOL_DESCRIPTION as DATABASE_TOOL_DESCRIPTION,
    TOOL_NAME as DATABASE_TOOL_NAME,
    DatabaseQueryTool,
    build_tool_parameters as build_database_tool_parameters,
)
from agents.tools.executor import ToolExecutor
from agents.tools.map_action import (
    TOOL_DESCRIPTION as MAP_ACTION_TOOL_DESCRIPTION,
    TOOL_NAME as MAP_ACTION_TOOL_NAME,
    TOOL_PARAMETERS as MAP_ACTION_TOOL_PARAMETERS,
    execute_map_action,
)
from agents.tools.load_skill import (
    TOOL_DESCRIPTION as LOAD_SKILL_TOOL_DESCRIPTION,
    TOOL_NAME as LOAD_SKILL_TOOL_NAME,
    TOOL_PARAMETERS as LOAD_SKILL_TOOL_PARAMETERS,
    load_skill,
)
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


def build_skill_catalog() -> SkillCatalog:
    return SkillCatalog(get_settings().skills.resolved_directory())


def build_tool_executor(
    skill_catalog: Optional[SkillCatalog] = None,
) -> ToolExecutor:
    settings = get_settings()
    catalog = skill_catalog if skill_catalog is not None else build_skill_catalog()
    executor = ToolExecutor(settings.tools.execution_timeout)
    executor.register(
        LOAD_SKILL_TOOL_NAME,
        LOAD_SKILL_TOOL_DESCRIPTION,
        partial(load_skill, catalog),
        LOAD_SKILL_TOOL_PARAMETERS,
    )
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
        DATABASE_TOOL_DESCRIPTION.format(max_rows=settings.tools.database.max_rows),
        database_tool.execute,
        build_database_tool_parameters(),
    )
    executor.register(
        OBSERVATION_TOOL_NAME,
        OBSERVATION_TOOL_DESCRIPTION,
        observation_tool.execute,
        OBSERVATION_TOOL_PARAMETERS,
    )
    executor.register(
        MAP_ACTION_TOOL_NAME,
        MAP_ACTION_TOOL_DESCRIPTION,
        execute_map_action,
        MAP_ACTION_TOOL_PARAMETERS,
    )
    executor.register_closer(rag_tool.close)
    executor.register_closer(database_tool.close)
    executor.register_closer(observation_tool.close)
    return executor
