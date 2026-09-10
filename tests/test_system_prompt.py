import unittest
from datetime import datetime
from hashlib import sha256
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from middleware.internal_request_auth import get_current_admin_id
from config.settings import get_settings
from main import app
from schemas.system_prompt import (
    ActiveSystemPromptSnapshot,
    SystemPromptVersionView,
    SystemPromptView,
)
from services.system_prompt_service import SystemPromptService, normalize_prompt


def request_with_identity(*, user_type: str = "ADMIN") -> Request:
    headers = [
        (b"x-user-id", b"7"),
        (b"x-auth-source", b"dh-gateway"),
        (b"x-user-type", user_type.encode("ascii")),
    ]
    return Request({"type": "http", "headers": headers})


class SystemPromptServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_newlines_and_rejects_invalid_content(self):
        self.assertEqual(normalize_prompt("  first\r\nsecond  "), "first\nsecond")
        with self.assertRaisesRegex(ValueError, "不能为空"):
            normalize_prompt(" \n ")
        with self.assertRaisesRegex(ValueError, "空字符"):
            normalize_prompt("valid\x00invalid")

    async def test_saves_normalized_draft_with_optimistic_revision(self):
        repository = AsyncMock()
        service = SystemPromptService(repository)

        await service.save_draft(
            "  managed\r\nprompt  ", expected_revision=3, operator_id=9
        )

        repository.save_draft.assert_awaited_once_with(
            "managed\nprompt", expected_revision=3, operator_id=9
        )

    async def test_returns_one_immutable_active_snapshot(self):
        content = "published prompt"
        snapshot = ActiveSystemPromptSnapshot(
            version_id=8,
            version_no=4,
            content=content,
            content_sha256=sha256(content.encode("utf-8")).hexdigest(),
        )
        repository = AsyncMock()
        repository.active_snapshot.return_value = snapshot

        result = await SystemPromptService(repository).active_snapshot()

        self.assertEqual(result, snapshot)


class SystemPromptAdminIdentityTest(unittest.TestCase):
    def test_accepts_gateway_admin_identity(self):
        self.assertEqual(get_current_admin_id(request_with_identity()), 7)

    def test_rejects_non_admin_identity(self):
        with self.assertRaises(HTTPException) as captured:
            get_current_admin_id(request_with_identity(user_type="USER"))
        self.assertEqual(captured.exception.status_code, 403)


class SystemPromptApiTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.headers = {
            get_settings().internal_request.header_name:
                get_settings().internal_request.token.get_secret_value(),
            "X-User-Id": "7",
            "X-Auth-Source": "dh-gateway",
            "X-User-Type": "ADMIN",
        }

    def test_returns_camel_case_admin_view(self):
        version = SystemPromptVersionView(
            id=11,
            version_no=2,
            content="published",
            content_sha256="a" * 64,
            release_note="test",
            source_version_no=None,
            published_by=7,
            published_at=datetime(2026, 9, 10),
            active=True,
        )
        view = SystemPromptView(
            prompt_key="main_agent",
            display_name="主智能体系统提示词",
            draft_content="draft",
            draft_revision=3,
            draft_updated_by=7,
            updated_at=datetime(2026, 9, 10),
            active_version=version,
        )
        with patch(
            "controller.system_prompt_controller.system_prompt_service.get",
            new=AsyncMock(return_value=view),
        ):
            response = self.client.get(
                "/admin/system-prompts/main", headers=self.headers
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["promptKey"], "main_agent")
        self.assertEqual(data["activeVersion"]["versionNo"], 2)

    def test_rejects_non_admin(self):
        response = self.client.get(
            "/admin/system-prompts/main",
            headers={**self.headers, "X-User-Type": "USER"},
        )
        self.assertEqual(response.status_code, 403)
