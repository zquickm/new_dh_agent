"""add durable run events for SSE replay

Revision ID: 0014
Revises: 0013
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # MySQL DDL implicitly commits. Inspect each independently committed piece so
    # an interrupted deployment can safely rerun this revision.
    run_columns: set[str] = set()
    message_columns: set[str] = set()
    tables: set[str] = set()
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        tables = set(inspector.get_table_names())
        run_columns = {
            item["name"] for item in inspector.get_columns("agent_conversation_run")
        }
        message_columns = {
            item["name"]
            for item in inspector.get_columns("agent_conversation_message")
        }

    if "last_event_sequence" not in run_columns:
        op.execute(sa.text("""
            ALTER TABLE `agent_conversation_run`
            ADD COLUMN `last_event_sequence` BIGINT UNSIGNED NOT NULL DEFAULT 0
                AFTER `owner_instance_id`
        """))
    if "stream_event_sequence" not in message_columns:
        op.execute(sa.text("""
            ALTER TABLE `agent_conversation_message`
            ADD COLUMN `stream_event_sequence` BIGINT UNSIGNED NOT NULL DEFAULT 0
                AFTER `status`
        """))
    if "agent_run_event" in tables:
        return
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_run_event` (
            `run_id` VARCHAR(36) NOT NULL,
            `sequence_no` BIGINT UNSIGNED NOT NULL,
            `event_type` VARCHAR(32) NOT NULL,
            `payload` JSON NOT NULL,
            `created_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`run_id`, `sequence_no`),
            CONSTRAINT `fk_agent_run_event_run`
                FOREIGN KEY (`run_id`)
                REFERENCES `agent_conversation_run` (`id`)
                ON DELETE CASCADE,
            INDEX `ix_agent_run_event_created` (`created_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_run_event`"))
    op.execute(sa.text("""
        ALTER TABLE `agent_conversation_message`
        DROP COLUMN `stream_event_sequence`
    """))
    op.execute(sa.text("""
        ALTER TABLE `agent_conversation_run`
        DROP COLUMN `last_event_sequence`
    """))
