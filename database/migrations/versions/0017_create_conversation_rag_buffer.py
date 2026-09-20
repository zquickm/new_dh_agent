"""create persistent conversation RAG buffer

Revision ID: 0017
Revises: 0016
"""
from typing import Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        if "agent_conversation_rag_buffer" in inspector.get_table_names():
            return
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_conversation_rag_buffer` (
            `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
            `conversation_id` VARCHAR(32) NOT NULL,
            `user_id` BIGINT NOT NULL,
            `identity_hash` VARCHAR(64) NOT NULL,
            `knowledge_id` VARCHAR(255) NULL,
            `document_id` VARCHAR(255) NULL,
            `chunk_id` VARCHAR(255) NULL,
            `source` VARCHAR(1000) NOT NULL,
            `title` VARCHAR(1000) NULL,
            `text` LONGTEXT NOT NULL,
            `url` LONGTEXT NULL,
            `page_index` INT NULL,
            `line` INT NULL,
            `page` INT NULL,
            `score` DOUBLE NOT NULL DEFAULT 0,
            `first_query` LONGTEXT NULL,
            `created_run_id` VARCHAR(36) NULL,
            `created_at` DATETIME(6) NOT NULL,
            `last_seen_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`id`),
            CONSTRAINT `fk_agent_rag_buffer_conversation_owner`
                FOREIGN KEY (`conversation_id`, `user_id`)
                REFERENCES `agent_conversation` (`id`, `user_id`)
                ON DELETE CASCADE,
            CONSTRAINT `uq_agent_rag_buffer_conversation_identity`
                UNIQUE (`conversation_id`, `user_id`, `identity_hash`),
            INDEX `ix_agent_rag_buffer_conversation_id`
                (`conversation_id`, `user_id`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_conversation_rag_buffer`"))
