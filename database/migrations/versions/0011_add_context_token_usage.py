"""add conversation run token usage

Revision ID: 0011
Revises: 0010
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("context_window_tokens", "INT UNSIGNED NULL"),
    ("input_budget_tokens", "INT UNSIGNED NULL"),
    ("estimated_input_tokens", "INT UNSIGNED NULL"),
    ("last_prompt_tokens", "INT UNSIGNED NULL"),
    ("last_completion_tokens", "INT UNSIGNED NULL"),
    ("prompt_tokens", "BIGINT UNSIGNED NOT NULL DEFAULT 0"),
    ("completion_tokens", "BIGINT UNSIGNED NOT NULL DEFAULT 0"),
    ("total_tokens", "BIGINT UNSIGNED NOT NULL DEFAULT 0"),
    ("history_messages_included", "INT UNSIGNED NOT NULL DEFAULT 0"),
    ("history_messages_dropped", "INT UNSIGNED NOT NULL DEFAULT 0"),
    ("tool_results_truncated", "INT UNSIGNED NOT NULL DEFAULT 0"),
    ("tokenizer_name", "VARCHAR(64) NULL"),
    ("token_usage_source", "VARCHAR(16) NULL"),
    ("token_round_number", "SMALLINT UNSIGNED NOT NULL DEFAULT 0"),
)


def upgrade() -> None:
    existing = set()
    if not context.is_offline_mode():
        existing = {
            item["name"]
            for item in sa.inspect(op.get_bind()).get_columns("agent_conversation_run")
        }
    clauses = [f"ADD COLUMN `{name}` {definition}" for name, definition in _COLUMNS if name not in existing]
    if clauses:
        op.execute(sa.text("ALTER TABLE `agent_conversation_run` " + ", ".join(clauses)))
    op.execute(sa.text("""
        UPDATE `agent_conversation`
        SET `context_message_limit` = 1000
        WHERE `context_message_limit` = 20
    """))
    op.execute(sa.text("""
        UPDATE `agent_conversation`
        SET `context_token_limit` = 131072
        WHERE `context_token_limit` IS NULL
    """))


def downgrade() -> None:
    clauses = [f"DROP COLUMN `{name}`" for name, _ in reversed(_COLUMNS)]
    op.execute(sa.text("ALTER TABLE `agent_conversation_run` " + ", ".join(clauses)))
