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
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False)
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
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=False), nullable=False)
    sequence_no: Mapped[int] = mapped_column(mysql.INTEGER(unsigned=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(mysql.LONGTEXT(), nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    images: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    recommend_questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
