from __future__ import annotations

from database.runtime import database_runtime
from repositories.system_prompt_repository import SystemPromptRepository
from schemas.system_prompt import (
    ActiveSystemPromptSnapshot,
    SystemPromptVersionPage,
    SystemPromptVersionView,
    SystemPromptView,
)
from services.context_service import SYSTEM_PROMPT


MAX_PROMPT_LENGTH = 50000


def normalize_prompt(content: str) -> str:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("系统提示词不能为空")
    if len(normalized) > MAX_PROMPT_LENGTH:
        raise ValueError(f"系统提示词不能超过 {MAX_PROMPT_LENGTH} 个字符")
    if "\x00" in normalized:
        raise ValueError("系统提示词不能包含空字符")
    return normalized


class SystemPromptService:
    def __init__(self, repository: SystemPromptRepository) -> None:
        self._repository = repository

    async def start(self) -> None:
        await self._repository.ensure_bootstrap(SYSTEM_PROMPT)

    async def active_snapshot(self) -> ActiveSystemPromptSnapshot:
        return await self._repository.active_snapshot()

    async def get(self) -> SystemPromptView:
        return await self._repository.get()

    async def save_draft(
        self, content: str, *, expected_revision: int, operator_id: int
    ) -> SystemPromptView:
        return await self._repository.save_draft(
            normalize_prompt(content),
            expected_revision=expected_revision,
            operator_id=operator_id,
        )

    async def publish(
        self,
        *,
        expected_revision: int,
        release_note: str | None,
        operator_id: int,
    ) -> SystemPromptVersionView:
        return await self._repository.publish(
            expected_revision=expected_revision,
            release_note=(release_note or "").strip() or None,
            operator_id=operator_id,
        )

    async def rollback(
        self,
        version_no: int,
        *,
        expected_revision: int,
        release_note: str | None,
        operator_id: int,
    ) -> SystemPromptVersionView:
        return await self._repository.rollback(
            version_no,
            expected_revision=expected_revision,
            release_note=(release_note or "").strip() or None,
            operator_id=operator_id,
        )

    async def list_versions(self, page: int, page_size: int) -> SystemPromptVersionPage:
        return await self._repository.list_versions(page, page_size)


system_prompt_service = SystemPromptService(SystemPromptRepository(database_runtime))
