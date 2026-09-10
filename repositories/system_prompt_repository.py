from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert

from database.models import SystemPromptModel, SystemPromptVersionModel
from database.runtime import DatabaseRuntime
from schemas.system_prompt import (
    ActiveSystemPromptSnapshot,
    SystemPromptVersionPage,
    SystemPromptVersionView,
    SystemPromptView,
)


MAIN_PROMPT_KEY = "main_agent"
SYSTEM_OPERATOR_ID = 0


class PromptRevisionConflict(RuntimeError):
    pass


class PromptVersionNotFound(LookupError):
    pass


class PromptContentUnchanged(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _checksum(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _version_view(
    item: SystemPromptVersionModel, *, active_version_id: int | None
) -> SystemPromptVersionView:
    return SystemPromptVersionView(
        id=item.id,
        version_no=item.version_no,
        content=item.content,
        content_sha256=item.content_sha256,
        release_note=item.release_note,
        source_version_no=item.source_version_no,
        published_by=item.published_by,
        published_at=item.published_at,
        active=item.id == active_version_id,
    )


class SystemPromptRepository:
    def __init__(self, runtime: DatabaseRuntime) -> None:
        self._runtime = runtime

    async def ensure_bootstrap(self, content: str) -> None:
        now = _now()
        async with self._runtime.session_factory() as session:
            async with session.begin():
                await session.execute(
                    insert(SystemPromptModel)
                    .values(
                        prompt_key=MAIN_PROMPT_KEY,
                        display_name="主智能体系统提示词",
                        draft_content=content,
                        draft_revision=1,
                        active_version_id=None,
                        draft_updated_by=SYSTEM_OPERATOR_ID,
                        created_at=now,
                        updated_at=now,
                    )
                    .prefix_with("IGNORE")
                )
                prompt = (
                    await session.execute(
                        select(SystemPromptModel)
                        .where(SystemPromptModel.prompt_key == MAIN_PROMPT_KEY)
                        .with_for_update()
                    )
                ).scalar_one()
                if prompt.active_version_id is not None:
                    return
                version = SystemPromptVersionModel(
                    prompt_id=prompt.id,
                    version_no=1,
                    content=prompt.draft_content,
                    content_sha256=_checksum(prompt.draft_content),
                    release_note="系统首次启动，从代码默认提示词初始化",
                    source_version_no=None,
                    published_by=SYSTEM_OPERATOR_ID,
                    published_at=now,
                )
                session.add(version)
                await session.flush()
                prompt.active_version_id = version.id
                prompt.updated_at = now

    async def active_snapshot(self) -> ActiveSystemPromptSnapshot:
        async with self._runtime.session_factory() as session:
            row = (
                await session.execute(
                    select(SystemPromptModel, SystemPromptVersionModel)
                    .join(
                        SystemPromptVersionModel,
                        SystemPromptVersionModel.id == SystemPromptModel.active_version_id,
                    )
                    .where(SystemPromptModel.prompt_key == MAIN_PROMPT_KEY)
                )
            ).one()
            version = row[1]
            return ActiveSystemPromptSnapshot(
                version_id=version.id,
                version_no=version.version_no,
                content=version.content,
                content_sha256=version.content_sha256,
            )

    async def get(self) -> SystemPromptView:
        async with self._runtime.session_factory() as session:
            prompt = (
                await session.execute(
                    select(SystemPromptModel).where(
                        SystemPromptModel.prompt_key == MAIN_PROMPT_KEY
                    )
                )
            ).scalar_one()
            active = await session.get(SystemPromptVersionModel, prompt.active_version_id)
            if active is None:
                raise RuntimeError("主系统提示词尚未发布")
            return SystemPromptView(
                prompt_key=prompt.prompt_key,
                display_name=prompt.display_name,
                draft_content=prompt.draft_content,
                draft_revision=prompt.draft_revision,
                draft_updated_by=prompt.draft_updated_by,
                updated_at=prompt.updated_at,
                active_version=_version_view(
                    active, active_version_id=prompt.active_version_id
                ),
            )

    async def save_draft(
        self, content: str, *, expected_revision: int, operator_id: int
    ) -> SystemPromptView:
        async with self._runtime.session_factory() as session:
            async with session.begin():
                prompt = await self._locked_prompt(session)
                self._check_revision(prompt, expected_revision)
                prompt.draft_content = content
                prompt.draft_revision += 1
                prompt.draft_updated_by = operator_id
                prompt.updated_at = _now()
        return await self.get()

    async def publish(
        self,
        *,
        expected_revision: int,
        release_note: str | None,
        operator_id: int,
    ) -> SystemPromptVersionView:
        async with self._runtime.session_factory() as session:
            async with session.begin():
                prompt = await self._locked_prompt(session)
                self._check_revision(prompt, expected_revision)
                active = await session.get(
                    SystemPromptVersionModel, prompt.active_version_id
                )
                draft_checksum = _checksum(prompt.draft_content)
                if active is not None and active.content_sha256 == draft_checksum:
                    raise PromptContentUnchanged("提示词草稿与当前线上版本相同")
                version_no = (
                    await session.scalar(
                        select(func.max(SystemPromptVersionModel.version_no)).where(
                            SystemPromptVersionModel.prompt_id == prompt.id
                        )
                    )
                    or 0
                ) + 1
                version = SystemPromptVersionModel(
                    prompt_id=prompt.id,
                    version_no=version_no,
                    content=prompt.draft_content,
                    content_sha256=draft_checksum,
                    release_note=release_note,
                    source_version_no=None,
                    published_by=operator_id,
                    published_at=_now(),
                )
                session.add(version)
                await session.flush()
                prompt.active_version_id = version.id
                prompt.draft_revision += 1
                prompt.updated_at = version.published_at
                active_version_id = version.id
        return _version_view(version, active_version_id=active_version_id)

    async def rollback(
        self,
        source_version_no: int,
        *,
        expected_revision: int,
        release_note: str | None,
        operator_id: int,
    ) -> SystemPromptVersionView:
        async with self._runtime.session_factory() as session:
            async with session.begin():
                prompt = await self._locked_prompt(session)
                self._check_revision(prompt, expected_revision)
                source = (
                    await session.execute(
                        select(SystemPromptVersionModel).where(
                            SystemPromptVersionModel.prompt_id == prompt.id,
                            SystemPromptVersionModel.version_no == source_version_no,
                        )
                    )
                ).scalar_one_or_none()
                if source is None:
                    raise PromptVersionNotFound(source_version_no)
                next_version = (
                    await session.scalar(
                        select(func.max(SystemPromptVersionModel.version_no)).where(
                            SystemPromptVersionModel.prompt_id == prompt.id
                        )
                    )
                    or 0
                ) + 1
                now = _now()
                version = SystemPromptVersionModel(
                    prompt_id=prompt.id,
                    version_no=next_version,
                    content=source.content,
                    content_sha256=source.content_sha256,
                    release_note=release_note or f"回滚至版本 v{source_version_no}",
                    source_version_no=source_version_no,
                    published_by=operator_id,
                    published_at=now,
                )
                session.add(version)
                await session.flush()
                prompt.draft_content = source.content
                prompt.draft_revision += 1
                prompt.draft_updated_by = operator_id
                prompt.active_version_id = version.id
                prompt.updated_at = now
                active_version_id = version.id
        return _version_view(version, active_version_id=active_version_id)

    async def list_versions(self, page: int, page_size: int) -> SystemPromptVersionPage:
        async with self._runtime.session_factory() as session:
            prompt = (
                await session.execute(
                    select(SystemPromptModel).where(
                        SystemPromptModel.prompt_key == MAIN_PROMPT_KEY
                    )
                )
            ).scalar_one()
            total = await session.scalar(
                select(func.count(SystemPromptVersionModel.id)).where(
                    SystemPromptVersionModel.prompt_id == prompt.id
                )
            )
            items = (
                await session.execute(
                    select(SystemPromptVersionModel)
                    .where(SystemPromptVersionModel.prompt_id == prompt.id)
                    .order_by(SystemPromptVersionModel.version_no.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).scalars().all()
            return SystemPromptVersionPage(
                total=int(total or 0),
                page=page,
                page_size=page_size,
                items=[
                    _version_view(item, active_version_id=prompt.active_version_id)
                    for item in items
                ],
            )

    @staticmethod
    async def _locked_prompt(session) -> SystemPromptModel:
        return (
            await session.execute(
                select(SystemPromptModel)
                .where(SystemPromptModel.prompt_key == MAIN_PROMPT_KEY)
                .with_for_update()
            )
        ).scalar_one()

    @staticmethod
    def _check_revision(prompt: SystemPromptModel, expected_revision: int) -> None:
        if prompt.draft_revision != expected_revision:
            raise PromptRevisionConflict(
                f"提示词草稿已被其他管理员修改，当前修订号为 {prompt.draft_revision}"
            )
