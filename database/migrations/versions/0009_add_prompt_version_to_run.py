"""record system prompt version on conversation runs

Revision ID: 0009
Revises: 0008
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE `agent_conversation_run`
        ADD COLUMN `system_prompt_version_id` BIGINT UNSIGNED NULL AFTER `model`,
        ADD COLUMN `system_prompt_version_no` BIGINT UNSIGNED NULL AFTER `system_prompt_version_id`,
        ADD COLUMN `system_prompt_sha256` VARCHAR(64) NULL AFTER `system_prompt_version_no`,
        ADD CONSTRAINT `fk_agent_run_system_prompt_version`
            FOREIGN KEY (`system_prompt_version_id`)
            REFERENCES `agent_system_prompt_version` (`id`),
        ADD INDEX `ix_agent_run_system_prompt_version` (`system_prompt_version_id`)
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE `agent_conversation_run`
        DROP FOREIGN KEY `fk_agent_run_system_prompt_version`,
        DROP INDEX `ix_agent_run_system_prompt_version`,
        DROP COLUMN `system_prompt_sha256`,
        DROP COLUMN `system_prompt_version_no`,
        DROP COLUMN `system_prompt_version_id`
    """))
