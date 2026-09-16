# 定义 Skill 管理接口的数据结构和任务内不可变启用快照。
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from schemas.system_prompt import CamelModel


class SkillView(CamelModel):
    skill_key: str
    name: str
    description: str
    enabled: bool
    revision: int
    updated_by: int
    updated_at: datetime


class UpdateSkillRequest(CamelModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    expected_revision: int = Field(..., ge=1, strict=True)


class ActiveSkillSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled_skill_names: frozenset[str]
