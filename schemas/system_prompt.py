from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class SystemPromptVersionView(CamelModel):
    id: int
    version_no: int
    content: str
    content_sha256: str
    release_note: Optional[str]
    source_version_no: Optional[int]
    published_by: int
    published_at: datetime
    active: bool = False


class SystemPromptView(CamelModel):
    prompt_key: str
    display_name: str
    draft_content: str
    draft_revision: int
    draft_updated_by: int
    updated_at: datetime
    active_version: SystemPromptVersionView


class SystemPromptVersionPage(CamelModel):
    total: int
    page: int
    page_size: int
    items: list[SystemPromptVersionView]


class SaveSystemPromptDraftRequest(CamelModel):
    content: str = Field(..., min_length=1, max_length=50000)
    expected_revision: int = Field(..., ge=1)


class PublishSystemPromptRequest(CamelModel):
    expected_revision: int = Field(..., ge=1)
    release_note: Optional[str] = Field(default=None, max_length=500)


class RollbackSystemPromptRequest(CamelModel):
    expected_revision: int = Field(..., ge=1)
    release_note: Optional[str] = Field(default=None, max_length=500)


class ActiveSystemPromptSnapshot(BaseModel):
    version_id: int
    version_no: int
    content: str
    content_sha256: str
