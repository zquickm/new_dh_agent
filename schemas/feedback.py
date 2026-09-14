from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


FeedbackType = Literal["LIKE", "DISLIKE"]
FeedbackStatus = Literal["ACTIVE", "WITHDRAWN"]


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


def _normalize_comment(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError("评论不能包含控制字符")
    return value


class SubmitFeedbackRequest(CamelModel):
    feedback_type: FeedbackType
    comment: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: Optional[str]) -> Optional[str]:
        return _normalize_comment(value)


class MessageFeedback(BaseModel):
    id: str
    feedback_type: FeedbackType
    comment: Optional[str] = None
    status: FeedbackStatus
    created_at: str
    updated_at: str


class AdminFeedbackItem(CamelModel):
    id: str
    feedback_type: FeedbackType
    comment: Optional[str] = None
    status: FeedbackStatus
    user_id: int
    username: str
    conversation_id: str
    conversation_title: str
    message_id: str
    run_id: Optional[str] = None
    question_preview: str
    answer_preview: str
    model: Optional[str] = None
    system_prompt_version_no: Optional[int] = None
    created_at: str
    updated_at: str


class AdminFeedbackPage(CamelModel):
    total: int
    page: int
    page_size: int
    items: list[AdminFeedbackItem] = Field(default_factory=list)


class AdminContextMessage(CamelModel):
    id: str
    sequence_no: int
    role: Literal["system", "user", "assistant"]
    content: str
    status: Literal["streaming", "completed", "failed"]
    error_code: Optional[str] = None
    run_id: Optional[str] = None
    sources: list[dict] = Field(default_factory=list)
    images: list[dict] = Field(default_factory=list)
    created_at: str
    target: bool = False


class AdminRunSummary(CamelModel):
    id: str
    status: Literal["running", "completed", "failed", "interrupted", "rejected"]
    model: Optional[str] = None
    system_prompt_version_no: Optional[int] = None
    system_prompt_sha256: Optional[str] = None
    started_at: str
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None


class AdminToolCallSummary(CamelModel):
    tool_call_id: str
    sequence_no: int
    tool_name: str
    status: Literal["running", "succeeded", "failed", "interrupted"]
    duration_ms: Optional[int] = None
    error_code: Optional[str] = None
    error_type: Optional[str] = None


class AdminFeedbackDetail(AdminFeedbackItem):
    context_messages: list[AdminContextMessage] = Field(default_factory=list)
    run: Optional[AdminRunSummary] = None
    tool_calls: list[AdminToolCallSummary] = Field(default_factory=list)
