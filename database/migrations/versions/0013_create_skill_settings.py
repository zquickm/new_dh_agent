# 创建 Skill 开关表，重复执行时保留已保存的状态。
from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS `agent_skill_setting` (
            `skill_key` VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
            `enabled` BOOLEAN NOT NULL DEFAULT 1,
            `revision` BIGINT UNSIGNED NOT NULL DEFAULT 1,
            `updated_by` BIGINT NOT NULL,
            `updated_at` DATETIME(6) NOT NULL,
            PRIMARY KEY (`skill_key`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS `agent_skill_setting`"))
