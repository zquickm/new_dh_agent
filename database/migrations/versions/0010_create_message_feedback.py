"""create assistant message feedback records

Revision ID: 0010
Revises: 0009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_message_feedback` (
            `id` VARCHAR(32) NOT NULL,
            `message_id` VARCHAR(32) NOT NULL,
            `feedback_type` VARCHAR(16) NOT NULL,
            `comment` VARCHAR(1000) NULL,
            `status` VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
            `username_snapshot` VARCHAR(64) NOT NULL,
            `created_at` DATETIME(6) NOT NULL,
            `updated_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `fk_agent_feedback_message`
                FOREIGN KEY (`message_id`)
                REFERENCES `agent_conversation_message` (`id`)
                ON DELETE CASCADE,
            UNIQUE KEY `uq_agent_feedback_message` (`message_id`),
            INDEX `ix_agent_feedback_status_updated` (`status`, `updated_at`, `id`),
            INDEX `ix_agent_feedback_type_updated` (`feedback_type`, `updated_at`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_message_feedback`"))
