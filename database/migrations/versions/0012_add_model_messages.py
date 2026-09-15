"""Persist model tool conversations separately from displayed answers."""
from alembic import context, op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode() and any(
        column["name"] == "model_messages"
        for column in sa.inspect(op.get_bind()).get_columns("agent_conversation_message")
    ):
        return
    op.add_column("agent_conversation_message", sa.Column("model_messages", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_conversation_message", "model_messages")
