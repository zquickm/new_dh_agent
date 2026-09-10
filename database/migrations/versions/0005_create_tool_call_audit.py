"""create durable tool call audit records

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # MySQL DDL implicitly commits, so keep this revision to one CREATE statement.
    # A failure cannot leave a partially added set of columns or indexes.
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_tool_call_audit` (
            `id` VARCHAR(32) NOT NULL,
            `run_id` VARCHAR(36) NOT NULL,
            `tool_call_id` VARCHAR(255) NOT NULL,
            `round_number` SMALLINT UNSIGNED NOT NULL,
            `sequence_no` INT UNSIGNED NOT NULL,
            `tool_name` VARCHAR(128) NOT NULL,
            `status` VARCHAR(16) NOT NULL,
            `arguments` JSON NOT NULL,
            `arguments_truncated` BOOLEAN NOT NULL DEFAULT 0,
            `result_summary` LONGTEXT NULL,
            `result_size_bytes` BIGINT UNSIGNED NULL,
            `result_sha256` VARCHAR(64) NULL,
            `result_truncated` BOOLEAN NOT NULL DEFAULT 0,
            `started_at` DATETIME(6) NOT NULL,
            `finished_at` DATETIME(6) NULL,
            `duration_ms` BIGINT UNSIGNED NULL,
            `error_code` VARCHAR(64) NULL,
            `error_type` VARCHAR(255) NULL,
            `error_message` LONGTEXT NULL,
            `failure_stage` VARCHAR(32) NULL,
            `retryable` BOOLEAN NOT NULL DEFAULT 0,
            `created_at` DATETIME(6) NOT NULL,
            `updated_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `fk_agent_tool_call_run`
                FOREIGN KEY (`run_id`)
                REFERENCES `agent_conversation_run` (`id`)
                ON DELETE CASCADE,
            CONSTRAINT `uq_agent_tool_call_run_call`
                UNIQUE (`run_id`, `tool_call_id`),
            CONSTRAINT `uq_agent_tool_call_run_sequence`
                UNIQUE (`run_id`, `sequence_no`),
            INDEX `ix_agent_tool_call_status_started` (`status`, `started_at`),
            INDEX `ix_agent_tool_call_name_started` (`tool_name`, `started_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_tool_call_audit`"))
