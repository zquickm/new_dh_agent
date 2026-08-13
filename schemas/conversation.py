from typing import Literal, Optional

from pydantic import BaseModel, Field


class CreateConversationRequest(BaseModel):
    title: str = "新会话"


class ConversationMessage(BaseModel):
    id: str
    role: Literal["system", "user", "assistant"]
    content: str
    created_at: str
    sources: list[dict] = Field(default_factory=list)
    images: list[dict] = Field(default_factory=list)
    recommend_questions: list[dict] = Field(default_factory=list)


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0


class ConversationDetail(ConversationSummary):
    context_enabled: bool = True
    context_message_limit: int = 20
    context_token_limit: Optional[int] = None
    messages: list[ConversationMessage] = Field(default_factory=list)


class ConversationPage(BaseModel):
    items: list[ConversationSummary] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False
