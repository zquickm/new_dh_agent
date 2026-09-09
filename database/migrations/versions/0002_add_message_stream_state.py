"""add durable assistant stream state

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_conversation_message",
        sa.Column("run_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "agent_conversation_message",
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'completed'"),
            nullable=False,
        ),
    )
    op.add_column(
        "agent_conversation_message",
        sa.Column("error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_conversation_message",
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_agent_message_run",
        "agent_conversation_message",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_message_run", table_name="agent_conversation_message")
    op.drop_column("agent_conversation_message", "updated_at")
    op.drop_column("agent_conversation_message", "error_code")
    op.drop_column("agent_conversation_message", "status")
    op.drop_column("agent_conversation_message", "run_id")
