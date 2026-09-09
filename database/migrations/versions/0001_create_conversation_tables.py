"""create conversation persistence tables

Revision ID: 0001
Revises:
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_conversation",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=False), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("context_enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("context_message_limit", mysql.SMALLINT(unsigned=True), server_default=sa.text("20"), nullable=False),
        sa.Column("context_token_limit", mysql.INTEGER(unsigned=True), nullable=True),
        sa.Column("map_context", sa.JSON(), nullable=True),
        sa.Column("message_count", mysql.INTEGER(unsigned=True), server_default=sa.text("0"), nullable=False),
        sa.Column("version", mysql.BIGINT(unsigned=True), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_conversation")),
        sa.UniqueConstraint("id", "user_id", name="uq_agent_conversation_id_user"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_agent_conversation_user_updated",
        "agent_conversation",
        ["user_id", "updated_at", "id"],
        unique=False,
    )
    op.create_table(
        "agent_conversation_message",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", mysql.BIGINT(unsigned=False), nullable=False),
        sa.Column("sequence_no", mysql.INTEGER(unsigned=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", mysql.LONGTEXT(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("images", sa.JSON(), nullable=False),
        sa.Column("recommend_questions", sa.JSON(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id", "user_id"],
            ["agent_conversation.id", "agent_conversation.user_id"],
            name="fk_agent_message_conversation_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_conversation_message")),
        sa.UniqueConstraint(
            "conversation_id",
            "sequence_no",
            name="uq_agent_message_conversation_sequence",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_agent_message_conversation_sequence",
        "agent_conversation_message",
        ["conversation_id", "sequence_no"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_message_conversation_sequence",
        table_name="agent_conversation_message",
    )
    op.drop_table("agent_conversation_message")
    op.drop_index(
        "ix_agent_conversation_user_updated", table_name="agent_conversation"
    )
    op.drop_table("agent_conversation")
