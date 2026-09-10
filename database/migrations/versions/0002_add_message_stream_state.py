"""add durable assistant stream state

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "agent_conversation_message"


def _schema_state() -> tuple[set[str], set[str]]:
    """Read the committed schema so a partially applied MySQL DDL can resume."""
    if context.is_offline_mode():
        return set(), set()
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    return columns, indexes


def _upgrade_clauses(columns: set[str], indexes: set[str]) -> list[str]:
    clauses: list[str] = []
    if "run_id" not in columns:
        clauses.append("ADD COLUMN `run_id` VARCHAR(36) NULL")
    if "status" not in columns:
        clauses.append(
            "ADD COLUMN `status` VARCHAR(16) NOT NULL DEFAULT 'completed'"
        )
    if "error_code" not in columns:
        clauses.append("ADD COLUMN `error_code` VARCHAR(64) NULL")
    if "updated_at" not in columns:
        clauses.append(
            "ADD COLUMN `updated_at` DATETIME(6) NOT NULL "
            "DEFAULT CURRENT_TIMESTAMP(6)"
        )
    if "ix_agent_message_run" not in indexes:
        clauses.append("ADD INDEX `ix_agent_message_run` (`run_id`)")
    return clauses


def _downgrade_clauses(columns: set[str], indexes: set[str]) -> list[str]:
    clauses: list[str] = []
    if "ix_agent_message_run" in indexes or context.is_offline_mode():
        clauses.append("DROP INDEX `ix_agent_message_run`")
    for column_name in ("updated_at", "error_code", "status", "run_id"):
        if column_name in columns or context.is_offline_mode():
            clauses.append(f"DROP COLUMN `{column_name}`")
    return clauses


def _execute_atomic_alter(clauses: list[str]) -> None:
    if clauses:
        # MySQL cannot transactionally roll back several DDL statements. Keeping
        # the revision in one ALTER gives MySQL 8 atomic-DDL failure semantics.
        op.execute(
            sa.text(f"ALTER TABLE `{TABLE_NAME}` " + ", ".join(clauses))
        )


def upgrade() -> None:
    columns, indexes = _schema_state()
    _execute_atomic_alter(_upgrade_clauses(columns, indexes))


def downgrade() -> None:
    columns, indexes = _schema_state()
    _execute_atomic_alter(_downgrade_clauses(columns, indexes))
