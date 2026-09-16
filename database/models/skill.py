# 保存每项本地 Skill 的启用状态和独立修订号。
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base


class SkillModel(Base):
    __tablename__ = "agent_skill_setting"
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

    skill_key: Mapped[str] = mapped_column(
        String(255, collation="utf8mb4_bin"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1")
    )
    revision: Mapped[int] = mapped_column(
        mysql.BIGINT(unsigned=True), nullable=False, server_default=text("1")
    )
    updated_by: Mapped[int] = mapped_column(mysql.BIGINT(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(mysql.DATETIME(fsp=6), nullable=False)
