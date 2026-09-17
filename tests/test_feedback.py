import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from config.settings import get_settings
from main import app
from middleware.internal_request_auth import get_current_user_identity
from repositories.mysql_feedback_repository import InvalidFeedbackTarget
from services.feedback_service import FeedbackService
from schemas.feedback import (
    AdminFeedbackDetail,
    AdminFeedbackItem,
    AdminFeedbackPage,
    AdminRunSummary,
    MessageFeedback,
    SubmitFeedbackRequest,
)


def identity_request(username: bytes = b"%E6%B5%8B%E8%AF%95%E7%94%A8%E6%88%B7"):
    return Request({
        "type": "http",
        "headers": [
            (b"x-user-id", b"101"),
            (b"x-auth-source", b"dh-gateway"),
            (b"x-username", username),
            (b"x-unit-id", b"10"),
            (b"x-department-id", b"23"),
            (b"x-role-codes", b"FORECASTER,REVIEWER"),
            (b"x-profile-version", b"3"),
        ],
    })


class FeedbackSchemaTest(unittest.TestCase):
    def test_normalizes_comment_and_accepts_camel_case(self):
        request = SubmitFeedbackRequest.model_validate({
            "feedbackType": "DISLIKE",
            "comment": "  数据年份不正确  ",
        })
        self.assertEqual(request.feedback_type, "DISLIKE")
        self.assertEqual(request.comment, "数据年份不正确")

    def test_rejects_control_characters(self):
        with self.assertRaisesRegex(ValueError, "控制字符"):
            SubmitFeedbackRequest(feedback_type="LIKE", comment="bad\x00text")


class FeedbackIdentityTest(unittest.TestCase):
    def test_decodes_trusted_gateway_username(self):
        identity = get_current_user_identity(identity_request())
        self.assertEqual(identity.user_id, "101")
        self.assertEqual(identity.username, "测试用户")
        self.assertEqual(identity.unit_id, "10")
        self.assertEqual(identity.department_id, "23")
        self.assertEqual(identity.role_codes, ("FORECASTER", "REVIEWER"))
        self.assertEqual(identity.profile_version, 3)

    def test_rejects_missing_username(self):
        request = Request({
            "type": "http",
            "headers": [
                (b"x-user-id", b"101"),
                (b"x-auth-source", b"dh-gateway"),
            ],
        })
        with self.assertRaises(HTTPException) as captured:
            get_current_user_identity(request)
        self.assertEqual(captured.exception.status_code, 401)


class FeedbackApiTest(unittest.TestCase):
    def setUp(self):
        settings = get_settings()
        self.client = TestClient(app)
        self.user_headers = {
            settings.internal_request.header_name:
                settings.internal_request.token.get_secret_value(),
            "X-User-Id": "101",
            "X-Username": "%E6%B5%8B%E8%AF%95%E7%94%A8%E6%88%B7",
            "X-Auth-Source": "dh-gateway",
            "X-User-Type": "USER",
        }
        self.admin_headers = {**self.user_headers, "X-User-Type": "ADMIN"}
        self.feedback = MessageFeedback(
            id="f" * 32,
            feedback_type="LIKE",
            comment="准确",
            status="ACTIVE",
            created_at="2026-09-14T08:00:00+00:00",
            updated_at="2026-09-14T08:00:00+00:00",
        )

    def test_submit_uses_gateway_identity(self):
        submit = AsyncMock(return_value=self.feedback)
        with patch("controller.feedback_controller.feedback_service.submit", submit):
            response = self.client.put(
                "/conversations/c1/messages/m1/feedback",
                headers=self.user_headers,
                json={"feedbackType": "LIKE", "comment": "准确"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["feedback_type"], "LIKE")
        self.assertEqual(submit.await_args.kwargs["user_id"], "101")
        self.assertEqual(submit.await_args.kwargs["username"], "测试用户")

    def test_withdraw_is_idempotent_at_api_boundary(self):
        withdraw = AsyncMock(return_value=None)
        with patch("controller.feedback_controller.feedback_service.withdraw", withdraw):
            response = self.client.delete(
                "/conversations/c1/messages/m1/feedback",
                headers=self.user_headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"])

    def test_withdraw_only_requires_stable_gateway_user_id(self):
        withdraw = AsyncMock(return_value=None)
        headers = dict(self.user_headers)
        del headers["X-Username"]
        with patch("controller.feedback_controller.feedback_service.withdraw", withdraw):
            response = self.client.delete(
                "/conversations/c1/messages/m1/feedback",
                headers=headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(withdraw.await_args.kwargs["user_id"], "101")

    def test_submit_maps_invalid_feedback_target_to_conflict(self):
        submit = AsyncMock(side_effect=InvalidFeedbackTarget("只能评价助手回复"))
        with patch("controller.feedback_controller.feedback_service.submit", submit):
            response = self.client.put(
                "/conversations/c1/messages/m1/feedback",
                headers=self.user_headers,
                json={"feedbackType": "LIKE"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], "只能评价助手回复")

    def test_admin_list_is_camel_case_and_rejects_user(self):
        item = AdminFeedbackItem(
            id="f" * 32,
            feedback_type="DISLIKE",
            comment="年份错误",
            status="ACTIVE",
            user_id=101,
            username="测试用户",
            conversation_id="c1",
            conversation_title="海温查询",
            message_id="m1",
            run_id="r1",
            question_preview="今年海温如何",
            answer_preview="根据历史数据",
            model="test-model",
            system_prompt_version_no=2,
            created_at="2026-09-14T08:00:00+00:00",
            updated_at="2026-09-14T08:00:00+00:00",
        )
        page = AdminFeedbackPage(total=1, page=1, page_size=20, items=[item])
        with patch(
            "controller.feedback_controller.feedback_service.list_admin",
            new=AsyncMock(return_value=page),
        ):
            response = self.client.get(
                "/admin/feedback?feedbackType=DISLIKE",
                headers=self.admin_headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pageSize"], 20)
        self.assertEqual(response.json()["data"]["items"][0]["userId"], 101)

        denied = self.client.get("/admin/feedback", headers=self.user_headers)
        self.assertEqual(denied.status_code, 403)

    def test_admin_detail_exposes_diagnostics_without_tool_payloads(self):
        item = AdminFeedbackItem(
            id="f" * 32,
            feedback_type="DISLIKE",
            comment=None,
            status="ACTIVE",
            user_id=101,
            username="测试用户",
            conversation_id="c1",
            conversation_title="海温查询",
            message_id="m1",
            run_id="r1",
            question_preview="问题",
            answer_preview="回答",
            model="test-model",
            system_prompt_version_no=2,
            created_at="2026-09-14T08:00:00+00:00",
            updated_at="2026-09-14T08:00:00+00:00",
        )
        detail = AdminFeedbackDetail(
            **item.model_dump(),
            context_messages=[],
            run=AdminRunSummary(
                id="r1",
                status="completed",
                model="test-model",
                started_at="2026-09-14T08:00:00+00:00",
            ),
            tool_calls=[],
        )
        with patch(
            "controller.feedback_controller.feedback_service.get_admin_detail",
            new=AsyncMock(return_value=detail),
        ):
            response = self.client.get(
                "/admin/feedback/" + "f" * 32,
                headers=self.admin_headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["run"]["model"], "test-model")
        self.assertEqual(response.json()["data"]["toolCalls"], [])


class FeedbackServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_reversed_admin_date_range(self):
        service = FeedbackService()
        with self.assertRaisesRegex(ValueError, "开始时间不能晚于结束时间"):
            await service.list_admin(
                page=1,
                page_size=20,
                feedback_type=None,
                status=None,
                user_id=None,
                keyword=None,
                start_at=datetime.fromisoformat("2026-09-15T00:00:00+08:00"),
                end_at=datetime.fromisoformat("2026-09-14T00:00:00+08:00"),
            )
