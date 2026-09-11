"""link active system prompt version

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE `agent_system_prompt`
        ADD CONSTRAINT `fk_agent_system_prompt_active_version`
        FOREIGN KEY (`active_version_id`)
        REFERENCES `agent_system_prompt_version` (`id`)
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        ALTER TABLE `agent_system_prompt`
        DROP FOREIGN KEY `fk_agent_system_prompt_active_version`
    """))
