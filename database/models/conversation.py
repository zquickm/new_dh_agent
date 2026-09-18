from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    ForeignKeyConstraint,
    Index,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class ConversationModel(Base):
    __tablename__ = "agent_conversation"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_agent_conversation_id_user"),
        Index("ix_agent_conversation_user_updated", "user_id", "updated_at", "id"),
        Index("ix_agent_conversation_department_updated", "data_unit_id", "data_department_id", "updated_at", "id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False)
    data_unit_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False, server_default=text("1"))
    data_department_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False, server_default=text("1"))
    creator_display_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    creator_role_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, server_default=text("(JSON_ARRAY('GENERAL_USER'))"))
    creator_profile_version: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False, server_default=text("1"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    context_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    context_message_limit: Mapped[int] = mapped_column(
        mysql.SMALLINT(unsigned=True), nullable=False, server_default=text("20")
    )
    context_token_limit: Mapped[Optional[int]] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=True
    )
    map_context: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    message_count: Mapped[int] = mapped_column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)


class ConversationMessageModel(Base):
    __tablename__ = "agent_conversation_message"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sequence_no",
            name="uq_agent_message_conversation_sequence",
        ),
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["agent_conversation.id", "agent_conversation.user_id"],
            name="fk_agent_message_conversation_owner",
            ondelete="CASCADE",
        ),
        Index("ix_agent_message_conversation_sequence", "conversation_id", "sequence_no"),
        Index("ix_agent_message_run", "run_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False)
    sequence_no: Mapped[int] = mapped_column(mysql.INTEGER(unsigned=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(mysql.LONGTEXT(), nullable=False)
    model_messages: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    recommend_questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'completed'")
    )
    stream_event_sequence: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=False, server_default=text("0")
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)


class MessageFeedbackModel(Base):
    __tablename__ = "agent_message_feedback"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    feedback_type: Mapped[str] = mapped_column(String(16), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'ACTIVE'")
    )
    username_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["message_id"],
            ["agent_conversation_message.id"],
            name="fk_agent_feedback_message",
            ondelete="CASCADE",
        ),
        UniqueConstraint("message_id", name="uq_agent_feedback_message"),
        Index("ix_agent_feedback_status_updated", "status", "updated_at", "id"),
        Index(
            "ix_agent_feedback_type_updated",
            "feedback_type",
            "updated_at",
            "id",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class ConversationRunModel(Base):
    __tablename__ = "agent_conversation_run"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["agent_conversation.id", "agent_conversation.user_id"],
            name="fk_agent_run_conversation_owner",
            ondelete="CASCADE",
        ),
        Index(
            "ix_agent_run_user_conversation_started",
            "user_id",
            "conversation_id",
            "started_at",
        ),
        Index("ix_agent_run_status_heartbeat", "status", "heartbeat_at"),
        Index("ix_agent_run_user_message", "user_message_id"),
        Index("ix_agent_run_assistant_message", "assistant_message_id"),
        Index("ix_agent_run_system_prompt_version", "system_prompt_version_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False)
    user_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    assistant_message_id: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    system_prompt_version_id: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    system_prompt_version_no: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    system_prompt_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    context_window_tokens: Mapped[Optional[int]] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    input_budget_tokens: Mapped[Optional[int]] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    estimated_input_tokens: Mapped[Optional[int]] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    last_prompt_tokens: Mapped[Optional[int]] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    last_completion_tokens: Mapped[Optional[int]] = mapped_column(mysql.INTEGER(unsigned=True), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), nullable=False, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), nullable=False, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), nullable=False, server_default=text("0"))
    history_messages_included: Mapped[int] = mapped_column(mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0"))
    history_messages_dropped: Mapped[int] = mapped_column(mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0"))
    tool_results_truncated: Mapped[int] = mapped_column(mysql.INTEGER(unsigned=True), nullable=False, server_default=text("0"))
    tokenizer_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    token_usage_source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    token_round_number: Mapped[int] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=False, server_default=text("0"))
    owner_instance_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    last_event_sequence: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    heartbeat_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    cancel_requested_at: Mapped[Optional[datetime]] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=True
    )
    cancel_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(mysql.LONGTEXT(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)


class RunEventModel(Base):
    __tablename__ = "agent_run_event"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["agent_conversation_run.id"],
            name="fk_agent_run_event_run",
            ondelete="CASCADE",
        ),
        Index("ix_agent_run_event_created", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence_no: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), primary_key=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)


class ToolCallAuditModel(Base):
    __tablename__ = "agent_tool_call_audit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id"],
            ["agent_conversation_run.id"],
            name="fk_agent_tool_call_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "tool_call_id", name="uq_agent_tool_call_run_call"),
        UniqueConstraint(
            "run_id", "sequence_no", name="uq_agent_tool_call_run_sequence"
        ),
        Index("ix_agent_tool_call_status_started", "status", "started_at"),
        Index("ix_agent_tool_call_name_started", "tool_name", "started_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    round_number: Mapped[int] = mapped_column(mysql.SMALLINT(unsigned=True), nullable=False)
    sequence_no: Mapped[int] = mapped_column(mysql.INTEGER(unsigned=True), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    arguments_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    result_summary: Mapped[Optional[str]] = mapped_column(mysql.LONGTEXT(), nullable=True)
    result_size_bytes: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    result_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    result_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    started_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=True
    )
    duration_ms: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_type: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(mysql.LONGTEXT(), nullable=True)
    failure_stage: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    retryable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
