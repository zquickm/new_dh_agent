from typing import Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(..., min_length=1)


class AgentRequest(BaseModel):
    question: str = Field(..., min_length=1)
    chat_history: list[ChatMessage] = Field(default_factory=list)
    conversation_id: Optional[str] = None
