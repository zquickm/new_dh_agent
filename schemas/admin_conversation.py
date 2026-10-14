from typing import Any, Optional

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class AdminDebugResponse(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AdminConversationPage(AdminDebugResponse):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class AdminConversationDetail(AdminDebugResponse):
    conversation: dict[str, Any]
    messages: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class AdminRunDetail(AdminDebugResponse):
    run: dict[str, Any]
    model_messages: Optional[list[dict[str, Any]]]
    system_prompt: Optional[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class AdminEventPage(AdminDebugResponse):
    items: list[dict[str, Any]]
    next_sequence: int
    has_more: bool
