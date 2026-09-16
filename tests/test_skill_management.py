# 验证 Skill 自动发现、独立状态、管理员接口及隔离 MySQL 持久化。
import asyncio
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from agents.skills import SkillCatalog
from config.settings import get_settings
from database.models.skill import SkillModel
from database.runtime import DatabaseRuntime
from main import app
from repositories.skill_repository import SkillNotFound, SkillRepository, SkillRevisionConflict
from schemas.skill import ActiveSkillSnapshot, SkillView
from services.skill_service import SkillService


class SkillServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repository = AsyncMock()
        self.states = {}

        async def ensure(names):
            for name in names:
                self.states.setdefault(name, SkillModel(
                    skill_key=name, enabled=True, revision=1,
                    updated_by=0, updated_at=datetime(2026, 9, 16),
                ))

        self.repository.ensure_skills.side_effect = ensure
        self.repository.get_states.side_effect = lambda names: {
            name: self.states[name] for name in names if name in self.states
        }
        self.service = SkillService(self.repository, SkillCatalog(self.root))
        self.add_skill("空间联动")
        self.add_skill("断面业务")

    def add_skill(self, name):
        path = self.root / name
        path.mkdir()
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name}说明\n---\n{name}操作正文",
            encoding="utf-8",
        )

    async def test_discovers_third_markdown_without_resetting_other_states(self):
        await self.service.start()
        self.states["断面业务"].enabled = False
        self.states["断面业务"].revision = 4
        before = await self.service.active_snapshot()
        self.add_skill("新增技能")
        restarted = SkillService(self.repository, SkillCatalog(self.root))
        await restarted.start()
        listed = {item.name: item for item in await restarted.list_skills()}
        self.assertEqual(set(listed), {"空间联动", "断面业务", "新增技能"})
        self.assertTrue(listed["新增技能"].enabled)
        self.assertFalse(listed["断面业务"].enabled)
        self.assertEqual(listed["断面业务"].revision, 4)
        self.assertEqual(before.enabled_skill_names, frozenset({"空间联动"}))
        self.assertEqual((await restarted.active_snapshot()).enabled_skill_names,
                         frozenset({"空间联动", "新增技能"}))

    async def test_snapshot_is_immutable_and_database_errors_propagate(self):
        snapshot = await self.service.active_snapshot()
        with self.assertRaises(ValueError):
            snapshot.enabled_skill_names = frozenset()
        self.repository.get_states.side_effect = RuntimeError("数据库不可用")
        with self.assertRaisesRegex(RuntimeError, "数据库不可用"):
            await self.service.active_snapshot()

    async def test_missing_state_does_not_silently_enable_skill(self):
        self.repository.get_states.side_effect = lambda names: {}
        with self.assertRaisesRegex(RuntimeError, "状态不完整"):
            await self.service.active_snapshot()

    async def test_unknown_skill_is_rejected_before_update(self):
        with self.assertRaises(SkillNotFound):
            await self.service.update("不存在", False, expected_revision=1, operator_id=7)
        self.repository.update.assert_not_awaited()


class SkillApiTest(unittest.TestCase):
    def setUp(self):
        self.service = AsyncMock()
        replacement = patch("controller.skill_controller.skill_service", self.service)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {
            get_settings().internal_request.header_name:
                get_settings().internal_request.token.get_secret_value(),
            "X-User-Id": "7", "X-Auth-Source": "dh-gateway", "X-User-Type": "ADMIN",
        }
        self.view = SkillView(
            skill_key="空间联动", name="空间联动", description="技能说明", enabled=False,
            revision=2, updated_by=7, updated_at=datetime(2026, 9, 16),
        )

    def test_admin_list_and_single_update_contract(self):
        self.service.list_skills.return_value = [self.view]
        response = self.client.get("/admin/skills", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["skillKey"], "空间联动")
        self.service.update.return_value = self.view
        response = self.client.put("/admin/skills/空间联动/enabled", headers=self.headers,
                                   json={"enabled": False, "expectedRevision": 1})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["enabled"])
        self.service.update.assert_awaited_once_with(
            "空间联动", False, expected_revision=1, operator_id=7,
        )

    def test_rejects_untrusted_missing_identity_and_non_admin(self):
        for headers, expected in (
            ({}, 403),
            ({get_settings().internal_request.header_name:
                get_settings().internal_request.token.get_secret_value()}, 401),
            ({**self.headers, "X-User-Type": "USER"}, 403),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.client.get("/admin/skills", headers=headers).status_code, expected)
                self.assertEqual(self.client.put(
                    "/admin/skills/空间联动/enabled", headers=headers,
                    json={"enabled": False, "expectedRevision": 1},
                ).status_code, expected)
        self.service.update.assert_not_awaited()

    def test_rejects_invalid_body_and_reports_conflicts(self):
        for body in (
            {"enabled": "false", "expectedRevision": 1},
            {"enabled": False, "expectedRevision": True},
            {"enabled": False, "expectedRevision": 0},
            {"enabled": False, "expectedRevision": 1, "otherSkill": False},
        ):
            self.assertEqual(self.client.put(
                "/admin/skills/空间联动/enabled", headers=self.headers, json=body,
            ).status_code, 422)
        for error, expected in ((SkillNotFound("不存在"), 404), (SkillRevisionConflict("修订冲突"), 409)):
            self.service.update.side_effect = error
            self.assertEqual(self.client.put(
                "/admin/skills/空间联动/enabled", headers=self.headers,
                json={"enabled": False, "expectedRevision": 1},
            ).status_code, expected)


@unittest.skipUnless(os.getenv("SKILL_TEST_DB_NAME"), "需要显式指定隔离 Skill 测试库")
class SkillMysqlTest(unittest.IsolatedAsyncioTestCase):
    async def test_independent_updates_conflicts_and_restart_persistence(self):
        settings = get_settings().persistence
        self.assertEqual(settings.name, os.environ["SKILL_TEST_DB_NAME"])
        self.assertTrue(settings.name.startswith("dh_skill_test_"))
        self.assertIn(settings.host, {"127.0.0.1", "localhost"})
        runtime = DatabaseRuntime()
        await runtime.start()
        names = (f"测试甲-{uuid4().hex}", f"测试乙-{uuid4().hex}")
        try:
            repository = SkillRepository(runtime)
            await repository.ensure_skills(names)
            await asyncio.gather(*(
                repository.update(name, False, expected_revision=1, operator_id=7)
                for name in names
            ))
            states = await repository.get_states(names)
            self.assertTrue(all(not item.enabled and item.revision == 2 for item in states.values()))
            results = await asyncio.gather(*(
                repository.update(names[0], True, expected_revision=2, operator_id=7)
                for _ in range(2)
            ), return_exceptions=True)
            self.assertEqual(sum(isinstance(item, SkillRevisionConflict) for item in results), 1)
            await runtime.close()
            await runtime.start()
            await repository.ensure_skills(names)
            states = await repository.get_states(names)
            self.assertTrue(states[names[0]].enabled)
            self.assertEqual(states[names[0]].revision, 3)
            self.assertFalse(states[names[1]].enabled)
            self.assertEqual(states[names[1]].revision, 2)
        finally:
            await runtime.close()
