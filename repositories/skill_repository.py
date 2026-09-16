# 持久化 Skill 独立状态，使用行锁和修订号防止并发覆盖。
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from database.models.skill import SkillModel
from database.runtime import DatabaseRuntime


class SkillNotFound(LookupError):
    pass


class SkillRevisionConflict(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SkillRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self._runtime = runtime

    async def ensure_skills(self, skill_names: tuple[str, ...]) -> None:
        if not skill_names:
            return
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(SkillModel).values([
                        {
                            "skill_key": name,
                            "enabled": True,
                            "revision": 1,
                            "updated_by": 0,
                            "updated_at": now,
                        }
                        for name in skill_names
                    ]).prefix_with("IGNORE")
                )

    async def get_states(self, skill_names: tuple[str, ...]) -> dict[str, SkillModel]:
        if not skill_names:
            return {}
        async with self._runtime.session_factory() as session:
            rows = (
                await session.execute(
                    select(SkillModel).where(SkillModel.skill_key.in_(skill_names))
                )
            ).scalars().all()
            return {row.skill_key: row for row in rows}

    async def update(
        self, skill_key: str, enabled: bool, *, expected_revision: int, operator_id: int
    ) -> SkillModel:
        async with self._runtime.session_factory() as session:
            async with session.begin():
                state = (
                    await session.execute(
                        select(SkillModel)
                        .where(SkillModel.skill_key == skill_key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if state is None:
                    raise SkillNotFound(skill_key)
                if state.revision != expected_revision:
                    raise SkillRevisionConflict(
                        f"该 Skill 已被其他管理员修改，当前修订号为 {state.revision}"
                    )
                state.enabled = enabled
                state.revision += 1
                state.updated_by = operator_id
                state.updated_at = _now()
        return state
