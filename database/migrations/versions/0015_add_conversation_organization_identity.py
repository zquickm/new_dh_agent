"""add organization identity snapshots to conversations

Revision ID: 0015
Revises: 0014
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE agent_conversation "
        "ADD COLUMN data_unit_id BIGINT NOT NULL DEFAULT 1 AFTER user_id, "
        "ADD COLUMN data_department_id BIGINT NOT NULL DEFAULT 1 AFTER data_unit_id, "
        "ADD COLUMN creator_display_name VARCHAR(100) NULL AFTER data_department_id, "
        "ADD COLUMN creator_role_codes JSON NOT NULL DEFAULT (JSON_ARRAY('GENERAL_USER')) AFTER creator_display_name, "
        "ADD COLUMN creator_profile_version BIGINT NOT NULL DEFAULT 1 AFTER creator_role_codes, "
        "ADD INDEX ix_agent_conversation_department_updated "
        "(data_unit_id, data_department_id, updated_at, id)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agent_conversation "
        "DROP INDEX ix_agent_conversation_department_updated, "
        "DROP COLUMN creator_profile_version, "
        "DROP COLUMN creator_role_codes, "
        "DROP COLUMN creator_display_name, "
        "DROP COLUMN data_department_id, "
        "DROP COLUMN data_unit_id"
    )
