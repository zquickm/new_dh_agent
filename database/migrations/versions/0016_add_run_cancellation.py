"""add cooperative conversation run cancellation

Revision ID: 0016
Revises: 0015
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns: set[str] = set()
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        columns = {
            item["name"]
            for item in inspector.get_columns("agent_conversation_run")
        }
    clauses = []
    if "cancel_requested_at" not in columns:
        clauses.append(
            "ADD COLUMN `cancel_requested_at` DATETIME(6) NULL AFTER `heartbeat_at`"
        )
    if "cancelled_at" not in columns:
        clauses.append(
            "ADD COLUMN `cancelled_at` DATETIME(6) NULL AFTER `cancel_requested_at`"
        )
    if "cancel_reason" not in columns:
        clauses.append(
            "ADD COLUMN `cancel_reason` VARCHAR(32) NULL AFTER `cancelled_at`"
        )
    if clauses:
        op.execute(sa.text(
            "ALTER TABLE `agent_conversation_run` " + ", ".join(clauses)
        ))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE `agent_conversation_run`
        DROP COLUMN `cancel_reason`,
        DROP COLUMN `cancelled_at`,
        DROP COLUMN `cancel_requested_at`
    """))
