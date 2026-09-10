"""create editable system prompt workspace

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_system_prompt` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            `prompt_key` VARCHAR(64) NOT NULL,
            `display_name` VARCHAR(128) NOT NULL,
            `draft_content` LONGTEXT NOT NULL,
            `draft_revision` BIGINT UNSIGNED NOT NULL DEFAULT 1,
            `active_version_id` BIGINT UNSIGNED NULL,
            `draft_updated_by` BIGINT NOT NULL,
            `created_at` DATETIME(6) NOT NULL,
            `updated_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `uq_agent_system_prompt_key` UNIQUE (`prompt_key`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_system_prompt`"))
