import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, Table, create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config.settings import get_settings
from database.models import (
    ConversationMessageModel, ConversationModel, ConversationRunModel,
    RunEventModel, SystemPromptVersionModel, ToolCallAuditModel,
)
from main import app
from repositories.mysql_admin_conversation_repository import MySQLAdminConversationRepository
from services.admin_conversation_service import AdminConversationService


class ReadSession:
    """Run the real ORM SELECTs on an isolated SQLite fixture, without async drivers."""
    def __init__(self, engine):
        self.session = Session(engine)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.session.close()

    async def execute(self, statement):
        return self.session.execute(statement)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def get(self, model, key):
        return self.session.get(model, key)


class DatabaseFixture:
    def setUp(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        metadata = MetaData()
        self.tables = {}
        # Only the test fixture adapts MySQL types/defaults; production metadata is untouched.
        for model in (ConversationModel, ConversationMessageModel, ConversationRunModel,
                      RunEventModel, ToolCallAuditModel, SystemPromptVersionModel):
            self.tables[model] = Table(model.__tablename__, metadata, *[
                Column(column.key, column.type.as_generic(), primary_key=column.primary_key, nullable=True)
                for column in model.__table__.columns
            ])
        metadata.create_all(self.engine)
        self.now = datetime(2026, 10, 13, 8)
        with self.engine.begin() as connection:
            def insert(model, **values):
                connection.execute(self.tables[model].insert().values(**values))

            for cid, uid, name, title, created in (
                ("c1", 101, "测试用户", "海温查询", datetime(2026, 10, 12, 8)),
                ("c2", 102, "其他用户", "水质查询", self.now),
                ("c3", 101, "测试用户", "100%覆盖", self.now),
            ):
                insert(ConversationModel, id=cid, user_id=uid, title=title,
                       creator_display_name=name, message_count=3 if cid == "c1" else 0,
                       map_context={"selection": "test"}, created_at=created, updated_at=self.now)
            insert(ConversationMessageModel, id="m1", conversation_id="c1", user_id=101,
                   sequence_no=1, role="user", content="查询海温", run_id=None,
                   created_at=self.now, updated_at=self.now, status="completed")
            self.model_messages = [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call1", "name": "database_query", "args": {"year": 2026}}]},
                {"role": "tool", "tool_call_id": "call1", "content": '{"rows":[{"value":25}]}'},
                {"role": "assistant", "content": "海温25度"},
            ]
            insert(ConversationMessageModel, id="m2", conversation_id="c1", user_id=101,
                   sequence_no=2, role="assistant", content="海温25度", run_id="r1",
                   model_messages=self.model_messages, sources=[{"source": "资料"}],
                   created_at=self.now, updated_at=self.now, status="completed")
            insert(ConversationMessageModel, id="m3", conversation_id="c1", user_id=101,
                   sequence_no=3, role="assistant", content="旧回答", run_id=None,
                   model_messages=None, created_at=self.now, updated_at=self.now, status="completed")
            # Corrupt ownership must not leak through conversation message queries.
            insert(ConversationMessageModel, id="foreign", conversation_id="c1", user_id=102,
                   sequence_no=4, role="assistant", content="其他用户消息", created_at=self.now)
            insert(ConversationRunModel, id="r1", conversation_id="c1", user_id=101,
                   user_message_id="m1", assistant_message_id="m2", status="completed",
                   total_tokens=200, started_at=self.now, system_prompt_version_id=7)
            insert(ConversationRunModel, id="r2", conversation_id="c2", user_id=102,
                   user_message_id="missing", assistant_message_id="missing", status="failed", started_at=self.now)
            insert(SystemPromptVersionModel, id=7, prompt_id=1, version_no=2, content="历史提示词",
                   content_sha256="abc", published_at=self.now)
            insert(ToolCallAuditModel, id="t2", run_id="r1", tool_call_id="call2", round_number=2,
                   sequence_no=2, tool_name="rag_search", status="failed", arguments={"query": "海温"},
                   arguments_truncated=False, result_summary="检索失败", result_truncated=False,
                   error_code="TIMEOUT", started_at=self.now)
            insert(ToolCallAuditModel, id="t1", run_id="r1", tool_call_id="call1", round_number=1,
                   sequence_no=1, tool_name="database_query", status="completed", arguments={"year": 2026},
                   arguments_truncated=True, result_summary="摘要", result_truncated=True, started_at=self.now)
            for sequence, kind in ((3, "done"), (1, "process"), (2, "retrieval")):
                insert(RunEventModel, run_id="r1", sequence_no=sequence, event_type=kind,
                       payload={"chunks": [{"text": "完整检索片段"}]} if kind == "retrieval" else {"text": kind},
                       created_at=self.now)
        runtime = SimpleNamespace(session_factory=lambda: ReadSession(self.engine))
        self.repository = MySQLAdminConversationRepository(runtime)
        self.service = AdminConversationService(self.repository)
        self.statements = []
        event.listen(self.engine, "before_cursor_execute", self.capture_sql)

    def capture_sql(self, connection, cursor, statement, parameters, context, many):
        self.statements.append(statement)

    def tearDown(self):
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in self.statements))
        self.engine.dispose()


class AdminConversationQueryTest(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def query(self, **overrides):
        params = dict(page=1, page_size=20, user_id=None, keyword=None, start_at=None,
                      end_at=None, time_field="updatedAt", sort_by="updatedAt", sort_order="desc")
        return await self.service.list_conversations(**{**params, **overrides})

    async def test_user_filter_literal_keyword_and_stable_pagination(self):
        result = await self.query(user_id=101)
        self.assertEqual([item["id"] for item in result.items], ["c3", "c1"])
        self.assertEqual((await self.query(keyword="测试用户")).total, 2)
        self.assertEqual([item["id"] for item in (await self.query(keyword="%")).items], ["c3"])
        pages = [await self.query(page=page, page_size=1) for page in (1, 2, 3)]
        self.assertEqual([result.items[0]["id"] for result in pages], ["c3", "c2", "c1"])
        self.assertNotIn("mapContext", pages[0].items[0])
        self.assertTrue(all("map_context" not in sql for sql in self.statements))

    async def test_time_zone_inclusive_boundaries_and_each_sort_direction(self):
        result = await self.query(time_field="createdAt", start_at=datetime.fromisoformat("2026-10-13T16:00:00+08:00"),
                                  end_at=datetime.fromisoformat("2026-10-13T08:00:00+00:00"))
        self.assertEqual(result.total, 2)
        self.assertEqual([item["id"] for item in (await self.query(sort_by="createdAt", sort_order="asc")).items], ["c1", "c3", "c2"])
        self.assertEqual([item["id"] for item in (await self.query(sort_by="userId", sort_order="desc")).items], ["c2", "c3", "c1"])
        self.assertEqual([item["id"] for item in (await self.query(sort_by="userId", sort_order="asc")).items], ["c3", "c1", "c2"])
        with self.assertRaisesRegex(ValueError, "开始时间"):
            await self.query(start_at=datetime(2026, 10, 14), end_at=self.now)

    async def test_message_pagination_includes_user_run_and_excludes_model_payload(self):
        result = await self.service.get_conversation("c1", page=1, page_size=1)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.messages[0]["id"], "m1")
        self.assertEqual(result.runs[0]["id"], "r1")
        self.assertNotIn("modelMessages", result.messages[0])
        self.assertEqual(result.conversation["userId"], "101")
        self.assertEqual(result.conversation["updatedAt"], "2026-10-13T08:00:00+00:00")
        self.assertEqual((await self.service.get_conversation("c1", page=3, page_size=1)).runs, [])
        self.assertIsNone(await self.service.get_conversation("missing", page=1, page_size=20))

    async def test_run_detail_uses_same_conversation_and_preserves_results(self):
        result = await self.service.get_run("c1", "r1", page=1, page_size=1)
        self.assertEqual(result.model_messages, self.model_messages)
        self.assertEqual(result.system_prompt["content"], "历史提示词")
        self.assertEqual(result.total, 2)
        self.assertEqual(result.tool_calls[0]["toolCallId"], "call1")
        self.assertTrue(result.tool_calls[0]["resultTruncated"])
        self.assertEqual((await self.service.get_run("c1", "r1", page=2, page_size=1)).tool_calls[0]["errorCode"], "TIMEOUT")
        self.assertIsNone(await self.service.get_run("c1", "r2", page=1, page_size=50))
        legacy = await self.service.get_run("c2", "r2", page=1, page_size=50)
        self.assertIsNone(legacy.model_messages)
        self.assertIsNone(legacy.system_prompt)
        self.assertEqual(legacy.tool_calls, [])

    async def test_event_cursor_preserves_rag_payload_and_rejects_foreign_run(self):
        first = await self.service.get_events("c1", "r1", after_sequence=0, page_size=2)
        self.assertEqual([item["sequenceNo"] for item in first.items], [1, 2])
        self.assertTrue(first.has_more)
        self.assertEqual(first.items[1]["payload"]["chunks"][0]["text"], "完整检索片段")
        last = await self.service.get_events("c1", "r1", after_sequence=first.next_sequence, page_size=2)
        self.assertEqual([item["sequenceNo"] for item in last.items], [3])
        self.assertFalse(last.has_more)
        empty = await self.service.get_events("c1", "r1", after_sequence=last.next_sequence, page_size=2)
        self.assertEqual(empty.next_sequence, 3)
        self.assertIsNone(await self.service.get_events("c1", "r2", after_sequence=0, page_size=2))


class AdminConversationApiTest(DatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.mock = patch("controller.admin_conversation_controller.service", self.service)
        self.mock.start()
        settings = get_settings()
        self.headers = {settings.internal_request.header_name: settings.internal_request.token.get_secret_value(),
                        "X-User-Id": "1", "X-Auth-Source": "dh-gateway", "X-User-Type": "ADMIN"}

    def tearDown(self):
        self.mock.stop()
        self.client.close()
        super().tearDown()

    def test_every_endpoint_requires_internal_gateway_identity_and_admin(self):
        for path in ("", "/c1", "/c1/runs/r1", "/c1/runs/r1/events"):
            url = "/admin/conversations" + path
            self.assertEqual(self.client.get(url).status_code, 403)
            missing = {key: value for key, value in self.headers.items() if key != "X-User-Id"}
            self.assertEqual(self.client.get(url, headers=missing).status_code, 401)
            self.assertEqual(self.client.get(url, headers={**self.headers, "X-User-Type": "USER"}).status_code, 403)
        self.assertEqual(self.statements, [])

    def test_envelope_camel_case_and_all_detail_paths(self):
        result = self.client.get("/admin/conversations?userId=101&sortBy=userId&sortOrder=asc", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"]["total"], 2)
        detail = self.client.get("/admin/conversations/c1?pageSize=1", headers=self.headers).json()["data"]
        self.assertEqual(detail["pageSize"], 1)
        self.assertNotIn("modelMessages", detail["messages"][0])
        run = self.client.get("/admin/conversations/c1/runs/r1", headers=self.headers).json()["data"]
        self.assertEqual(run["modelMessages"], self.model_messages)
        self.assertTrue(run["toolCalls"][0]["argumentsTruncated"])
        events = self.client.get("/admin/conversations/c1/runs/r1/events?pageSize=2", headers=self.headers).json()["data"]
        self.assertTrue(events["hasMore"])
        self.assertEqual(events["nextSequence"], 2)

    def test_missing_and_cross_conversation_records_return_404(self):
        for suffix in ("/missing", "/c1/runs/r2", "/c1/runs/r2/events"):
            self.assertEqual(self.client.get("/admin/conversations" + suffix, headers=self.headers).status_code, 404)

    def test_invalid_filters_and_pagination_are_rejected(self):
        for query in ("page=0", "pageSize=101", "userId=0", "userId=9223372036854775808",
                      "sortBy=content", "sortOrder=invalid", "timeField=status",
                      "startAt=2026-10-14T00:00:00Z&endAt=2026-10-13T00:00:00Z"):
            self.assertEqual(self.client.get("/admin/conversations?" + query, headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/admin/conversations/c1/runs/r1/events?afterSequence=-1", headers=self.headers).status_code, 422)
        self.assertEqual(self.statements, [])
