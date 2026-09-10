"""create durable conversation run records

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One CREATE statement gives MySQL 8 atomic-DDL failure semantics.
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_conversation_run` (
            `id` VARCHAR(36) NOT NULL,
            `conversation_id` VARCHAR(32) NOT NULL,
            `user_id` BIGINT NOT NULL,
            `user_message_id` VARCHAR(32) NOT NULL,
            `assistant_message_id` VARCHAR(32) NOT NULL,
            `status` VARCHAR(16) NOT NULL,
            `model` VARCHAR(128) NULL,
            `owner_instance_id` VARCHAR(36) NULL,
            `started_at` DATETIME(6) NOT NULL,
            `finished_at` DATETIME(6) NULL,
            `duration_ms` BIGINT UNSIGNED NULL,
            `heartbeat_at` DATETIME(6) NOT NULL,
            `error_code` VARCHAR(64) NULL,
            `error_type` VARCHAR(255) NULL,
            `error_message` LONGTEXT NULL,
            `created_at` DATETIME(6) NOT NULL,
            `updated_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `fk_agent_run_conversation_owner`
                FOREIGN KEY (`conversation_id`, `user_id`)
                REFERENCES `agent_conversation` (`id`, `user_id`)
                ON DELETE CASCADE,
            INDEX `ix_agent_run_user_conversation_started`
                (`user_id`, `conversation_id`, `started_at`),
            INDEX `ix_agent_run_status_heartbeat` (`status`, `heartbeat_at`),
            INDEX `ix_agent_run_user_message` (`user_message_id`),
            INDEX `ix_agent_run_assistant_message` (`assistant_message_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_conversation_run`"))
