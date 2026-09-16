# 合并本地 Skill 目录和数据库开关，为管理界面与每次任务提供一致状态。
from __future__ import annotations

from agents.skills import SkillCatalog, SkillSummary
from agents.tools.registry import build_skill_catalog
from database.models.skill import SkillModel
from database.runtime import database_runtime
from repositories.skill_repository import SkillNotFound, SkillRepository
from schemas.skill import ActiveSkillSnapshot, SkillView


class SkillService:
    def __init__(self, repository: SkillRepository, catalog: SkillCatalog) -> None:
        self._repository = repository
        self._catalog = catalog

    def _summaries(self) -> tuple[SkillSummary, ...]:
        summaries = self._catalog.summaries()
        if any(len(item.name) > 255 for item in summaries):
            raise ValueError("Skill 名称不能超过数据库支持的 255 个字符")
        return summaries

    async def start(self) -> None:
        await self._repository.ensure_skills(tuple(item.name for item in self._summaries()))

    async def list_skills(self) -> list[SkillView]:
        summaries = self._summaries()
        names = tuple(item.name for item in summaries)
        states = await self._repository.get_states(names)
        missing = tuple(name for name in names if name not in states)
        if missing:
            await self._repository.ensure_skills(missing)
            states = await self._repository.get_states(names)
        if set(states) != set(names):
            raise RuntimeError("Skill 状态不完整，请检查数据库初始化")
        return [self._view(item, states[item.name]) for item in summaries]

    async def active_snapshot(self) -> ActiveSkillSnapshot:
        return ActiveSkillSnapshot(
            enabled_skill_names=frozenset(
                item.skill_key for item in await self.list_skills() if item.enabled
            )
        )

    async def update(
        self, skill_key: str, enabled: bool, *, expected_revision: int, operator_id: int
    ) -> SkillView:
        summary = next((item for item in self._summaries() if item.name == skill_key), None)
        if summary is None:
            raise SkillNotFound(skill_key)
        if skill_key not in await self._repository.get_states((skill_key,)):
            await self._repository.ensure_skills((skill_key,))
        state = await self._repository.update(
            skill_key, enabled, expected_revision=expected_revision, operator_id=operator_id
        )
        return self._view(summary, state)

    @staticmethod
    def _view(summary: SkillSummary, state: SkillModel) -> SkillView:
        return SkillView(
            skill_key=summary.name,
            name=summary.name,
            description=summary.description,
            enabled=state.enabled,
            revision=state.revision,
            updated_by=state.updated_by,
            updated_at=state.updated_at,
        )


skill_service = SkillService(SkillRepository(database_runtime), build_skill_catalog())
