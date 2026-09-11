"""create immutable system prompt versions

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_system_prompt_version` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            `prompt_id` BIGINT UNSIGNED NOT NULL,
            `version_no` BIGINT UNSIGNED NOT NULL,
            `content` LONGTEXT NOT NULL,
            `content_sha256` VARCHAR(64) NOT NULL,
            `release_note` VARCHAR(500) NULL,
            `source_version_no` BIGINT UNSIGNED NULL,
            `published_by` BIGINT NOT NULL,
            `published_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `fk_agent_prompt_version_prompt`
                FOREIGN KEY (`prompt_id`) REFERENCES `agent_system_prompt` (`id`),
            CONSTRAINT `uq_agent_prompt_version_number`
                UNIQUE (`prompt_id`, `version_no`),
            INDEX `ix_agent_prompt_version_published` (`prompt_id`, `published_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_system_prompt_version`"))
