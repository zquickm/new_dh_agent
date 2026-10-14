from typing import Literal, Optional

from pydantic import BaseModel, Field

from schemas.feedback import MessageFeedback
from schemas.reasoning import ReasoningSnapshot


class CreateConversationRequest(BaseModel):
    title: str = "新会话"


class ConversationMessage(BaseModel):
    id: str
    role: Literal["system", "user", "assistant"]
    content: str
    reasoning: ReasoningSnapshot = Field(default_factory=ReasoningSnapshot)
    reasoning_event_id: int = 0
    answer_origin: Literal["model", "exact_qa"] = "model"
    exact_qa: Optional[dict] = None
    model_messages: Optional[list[dict]] = Field(default=None, exclude=True)
    created_at: str
    updated_at: Optional[str] = None
    run_id: Optional[str] = None
    status: Literal["streaming", "completed", "failed", "cancelled"] = "completed"
    stream_event_id: int = 0
    error_code: Optional[str] = None
    sources: list[dict] = Field(default_factory=list)
    images: list[dict] = Field(default_factory=list)
    recommend_questions: list[dict] = Field(default_factory=list)
    feedback: Optional[MessageFeedback] = None


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    is_generating: bool = False


class ContextUsage(BaseModel):
    context_window_tokens: int = 131072
    input_budget_tokens: int = 120832
    estimated_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    used_tokens: int = 0
    usage_percent: float = 0
    history_messages_included: int = 0
    history_messages_dropped: int = 0
    tool_results_truncated: int = 0
    tokenizer: str = "deepseek-v4-official"
    round_number: int = 0
    source: Literal["estimated", "upstream"] = "estimated"
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_total_tokens: int = 0


class ConversationDetail(ConversationSummary):
    context_enabled: bool = True
    context_message_limit: int = 1000
    context_token_limit: Optional[int] = 131072
    context_usage: ContextUsage = Field(default_factory=ContextUsage)
    messages: list[ConversationMessage] = Field(default_factory=list)


class ConversationPage(BaseModel):
    items: list[ConversationSummary] = Field(default_factory=list)
    next_cursor: Optional[str] = None
    has_more: bool = False


class ConversationRun(BaseModel):
    id: str
    conversation_id: str
    user_message_id: str
    assistant_message_id: str
    status: Literal[
        "running", "cancel_requested", "completed", "failed", "interrupted",
        "rejected", "cancelled",
    ]
    model: Optional[str] = None
    system_prompt_version_id: Optional[int] = None
    system_prompt_version_no: Optional[int] = None
    system_prompt_sha256: Optional[str] = None
    context_window_tokens: Optional[int] = None
    input_budget_tokens: Optional[int] = None
    estimated_input_tokens: Optional[int] = None
    last_prompt_tokens: Optional[int] = None
    last_completion_tokens: Optional[int] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    history_messages_included: int = 0
    history_messages_dropped: int = 0
    tool_results_truncated: int = 0
    tokenizer_name: Optional[str] = None
    token_usage_source: Optional[Literal["estimated", "upstream"]] = None
    token_round_number: int = 0
    last_event_id: int = 0
    started_at: str
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    heartbeat_at: str
    cancel_requested_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    cancel_reason: Optional[str] = None
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str


class RunStreamEvent(BaseModel):
    id: int
    event: str
    data: dict = Field(default_factory=dict)


class ToolCallAudit(BaseModel):
    id: str
    run_id: str
    tool_call_id: str
    round_number: int
    sequence_no: int
    tool_name: str
    status: Literal["running", "succeeded", "failed", "interrupted"]
    arguments: dict = Field(default_factory=dict)
    arguments_truncated: bool = False
    result_summary: Optional[str] = None
    result_size_bytes: Optional[int] = None
    result_sha256: Optional[str] = None
    result_truncated: bool = False
    started_at: str
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    failure_stage: Optional[str] = None
    retryable: bool = False
    created_at: str
    updated_at: str
