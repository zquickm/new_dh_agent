"""Shared reasoning snapshots and fixed-answer provenance.

Revision ID: 0019
Revises: 0018
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    existing = set() if context.is_offline_mode() else {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("agent_conversation_message")
    }
    # MySQL DDL commits per statement: resume safely after a partial upgrade.
    for column in [
        sa.Column("reasoning_json", sa.JSON(), nullable=True),
        sa.Column("reasoning_event_sequence", mysql.BIGINT(unsigned=True), nullable=False, server_default="0"),
        sa.Column("answer_origin", sa.String(16), nullable=False, server_default="model"),
        sa.Column("exact_qa_json", sa.JSON(), nullable=True),
    ]:
        if column.name not in existing:
            op.add_column("agent_conversation_message", column)


def downgrade():
    op.drop_column("agent_conversation_message", "exact_qa_json")
    op.drop_column("agent_conversation_message", "answer_origin")
    op.drop_column("agent_conversation_message", "reasoning_json")
    op.drop_column("agent_conversation_message", "reasoning_event_sequence")
