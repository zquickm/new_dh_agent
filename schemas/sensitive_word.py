from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


Status = Annotated[StrictInt, Field(ge=0, le=1)]
CategoryName = Annotated[str, Field(min_length=1, max_length=128)]
WordText = Annotated[str, Field(min_length=1, max_length=255)]
PositiveId = Annotated[int, Field(gt=0)]


class AdminRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", "word", mode="before", check_fields=False)
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class CategoryCreateRequest(AdminRequest):
    name: CategoryName
    status: Status = 1


class WordCreateRequest(AdminRequest):
    category_id: PositiveId
    word: WordText
    status: Status = 1


class PatchRequest(AdminRequest):
    @model_validator(mode="before")
    @classmethod
    def require_changes(cls, value: Any) -> Any:
        if isinstance(value, dict):
            if not value:
                raise ValueError("至少提供一个要修改的字段")
            if any(item is None for item in value.values()):
                raise ValueError("修改字段不能为 null")
        return value


class CategoryUpdateRequest(PatchRequest):
    name: Optional[CategoryName] = None
    status: Optional[Status] = None


class WordUpdateRequest(PatchRequest):
    category_id: Optional[PositiveId] = None
    word: Optional[WordText] = None
    status: Optional[Status] = None


class WordImportRequest(AdminRequest):
    category_id: PositiveId
    csv_text: str = Field(min_length=1)
    dry_run: bool = True


class SensitiveCheckRequest(AdminRequest):
    text: str
