from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class SystemPromptModel(Base):
    __tablename__ = "agent_system_prompt"
    __table_args__ = (
        UniqueConstraint("prompt_key", name="uq_agent_system_prompt_key"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), primary_key=True)
    prompt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    draft_content: Mapped[str] = mapped_column(mysql.LONGTEXT(), nullable=False)
    draft_revision: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=False, server_default=text("1")
    )
    active_version_id: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    draft_updated_by: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=False), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)


class SystemPromptVersionModel(Base):
    __tablename__ = "agent_system_prompt_version"
    __table_args__ = (
        UniqueConstraint(
            "prompt_id", "version_no", name="uq_agent_prompt_version_number"
        ),
        Index("ix_agent_prompt_version_published", "prompt_id", "published_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), primary_key=True)
    prompt_id: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True),
        ForeignKey("agent_system_prompt.id", name="fk_agent_prompt_version_prompt"),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(mysql.BIGINT(unsigned=True), nullable=False)
    content: Mapped[str] = mapped_column(mysql.LONGTEXT(), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    release_note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    source_version_no: Mapped[Optional[int]] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=True
    )
    published_by: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=False), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(
        mysql.DATETIME(fsp=6), nullable=False
    )
