"""record system prompt version on conversation runs

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns, indexes, foreign_keys = set(), set(), set()
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        columns = {item["name"] for item in inspector.get_columns("agent_conversation_run")}
        indexes = {item["name"] for item in inspector.get_indexes("agent_conversation_run")}
        foreign_keys = {item["name"] for item in inspector.get_foreign_keys("agent_conversation_run")}

    clauses = []
    for name, definition in (
        ("system_prompt_version_id", "BIGINT UNSIGNED NULL AFTER `model`"),
        ("system_prompt_version_no", "BIGINT UNSIGNED NULL AFTER `system_prompt_version_id`"),
        ("system_prompt_sha256", "VARCHAR(64) NULL AFTER `system_prompt_version_no`"),
    ):
        if name not in columns:
            clauses.append(f"ADD COLUMN `{name}` {definition}")
    if "fk_agent_run_system_prompt_version" not in foreign_keys:
        clauses.append(
            "ADD CONSTRAINT `fk_agent_run_system_prompt_version` "
            "FOREIGN KEY (`system_prompt_version_id`) "
            "REFERENCES `agent_system_prompt_version` (`id`)"
        )
    if "ix_agent_run_system_prompt_version" not in indexes:
        clauses.append("ADD INDEX `ix_agent_run_system_prompt_version` (`system_prompt_version_id`)")
    if clauses:
        op.execute(sa.text("ALTER TABLE `agent_conversation_run` " + ", ".join(clauses)))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE `agent_conversation_run`
        DROP FOREIGN KEY `fk_agent_run_system_prompt_version`,
        DROP INDEX `ix_agent_run_system_prompt_version`,
        DROP COLUMN `system_prompt_sha256`,
        DROP COLUMN `system_prompt_version_no`,
        DROP COLUMN `system_prompt_version_id`
    """))
