"""backfill run records for existing messages

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # This revision is DML-only, so transaction_per_migration can roll it back.
    op.execute(sa.text("""
        INSERT INTO `agent_conversation_run` (
            `id`, `conversation_id`, `user_id`,
            `user_message_id`, `assistant_message_id`, `status`,
            `model`, `owner_instance_id`, `started_at`, `finished_at`,
            `duration_ms`, `heartbeat_at`, `error_code`, `error_type`,
            `error_message`, `created_at`, `updated_at`
        )
        SELECT
            messages.`run_id`,
            messages.`conversation_id`,
            messages.`user_id`,
            MAX(CASE WHEN messages.`role` = 'user' THEN messages.`id` END),
            MAX(CASE WHEN messages.`role` = 'assistant' THEN messages.`id` END),
            CASE
                WHEN MAX(CASE WHEN messages.`role` = 'assistant'
                              THEN messages.`status` END) = 'streaming'
                    THEN 'running'
                WHEN MAX(CASE WHEN messages.`role` = 'assistant'
                              THEN messages.`error_code` END) = 'AGENT_INTERRUPTED'
                    THEN 'interrupted'
                WHEN MAX(CASE WHEN messages.`role` = 'assistant'
                              THEN messages.`error_code` END) =
                     'SENSITIVE_INPUT_REJECTED'
                    THEN 'rejected'
                WHEN MAX(CASE WHEN messages.`role` = 'assistant'
                              THEN messages.`status` END) = 'failed'
                    THEN 'failed'
                ELSE 'completed'
            END,
            NULL,
            NULL,
            MIN(messages.`created_at`),
            CASE MAX(CASE WHEN messages.`role` = 'assistant'
                          THEN messages.`status` END)
                WHEN 'streaming' THEN NULL
                ELSE MAX(messages.`updated_at`)
            END,
            CASE MAX(CASE WHEN messages.`role` = 'assistant'
                          THEN messages.`status` END)
                WHEN 'streaming' THEN NULL
                ELSE TIMESTAMPDIFF(
                    MICROSECOND,
                    MIN(messages.`created_at`),
                    MAX(messages.`updated_at`)
                ) DIV 1000
            END,
            MAX(messages.`updated_at`),
            MAX(CASE WHEN messages.`role` = 'assistant'
                     THEN messages.`error_code` END),
            NULL,
            NULL,
            MIN(messages.`created_at`),
            MAX(messages.`updated_at`)
        FROM `agent_conversation_message` AS messages
        LEFT JOIN `agent_conversation_run` AS runs
            ON runs.`id` = messages.`run_id`
        WHERE messages.`run_id` IS NOT NULL
          AND runs.`id` IS NULL
        GROUP BY
            messages.`run_id`, messages.`conversation_id`, messages.`user_id`
        HAVING
            MAX(CASE WHEN messages.`role` = 'user' THEN 1 ELSE 0 END) = 1
            AND MAX(CASE WHEN messages.`role` = 'assistant' THEN 1 ELSE 0 END) = 1
    """))


def downgrade() -> None:
    # 0003 drops the table; deleting indistinguishable live rows here would lose data.
    pass
