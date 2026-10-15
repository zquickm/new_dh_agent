import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.rag_search import RagSearchTool
from config.settings import RagKnowledgeBaseSettings, get_settings
from repositories.mysql_conversation_repository import MySQLConversationRepository
from services.rag_debug_service import RagDebugWriter, rag_debug_scope, record_rag_debug, restore_capture
from tests.test_admin_conversation import DatabaseFixture, ReadSession
from tests.test_agent_loop import FakeLlm
from tests.test_rag_reranker import FakeResponse, FakeReranker, FakeSession, make_backend


class EventStore:
    def __init__(self):
        self.events = []
        self.fail_once = False

    async def append_run_event(self, run_id, event_type, data, *, user_id):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("test storage unavailable")
        self.events.append((event_type, data))

    def restore(self, stage):
        manifest = next(data for kind, data in self.events if kind == "rag_debug" and data["stage"] == stage)
        parts = [data for kind, data in self.events if kind == "rag_debug_part" and data["capture_id"] == manifest["capture_id"]]
        return restore_capture(manifest, parts)


class RagDebugCaptureTest(unittest.IsolatedAsyncioTestCase):
    async def test_inline_integrity_survives_database_json_key_reordering(self):
        store = EventStore()
        await RagDebugWriter(store, "run", "101").record("query", {"z": 1, "a": {"y": 2, "b": 3}})
        kind, manifest = store.events[0]
        manifest["data"] = {"a": {"b": 3, "y": 2}, "z": 1}
        self.assertTrue(restore_capture(manifest, [])["complete"])

    async def test_large_response_roundtrip_redaction_and_incomplete_parts(self):
        store = EventStore()
        writer = RagDebugWriter(store, "run", "101")
        body = {"data": {"records": [{"text": "海洋资料😀" * 25000, "score": 0.2}]},
                "api_key": "secret", "nested": {"Authorization": "Bearer secret"}}
        await writer.record("response", {"body": body, "tool_call_id": "call"})
        restored = store.restore("response")
        self.assertTrue(restored["complete"])
        self.assertGreater(restored["part_count"], 1)
        self.assertEqual(restored["data"]["body"]["data"], body["data"])
        self.assertNotIn("secret", json.dumps(restored))
        manifest = next(data for kind, data in store.events if kind == "rag_debug")
        parts = [data for kind, data in store.events if kind == "rag_debug_part"]
        self.assertFalse(restore_capture(manifest, parts[:-1])["complete"])
        tampered = [{**parts[0], "text": "changed"}, *parts[1:]]
        self.assertFalse(restore_capture(manifest, tampered)["complete"])

    async def test_storage_failure_does_not_abort_retrieval_and_is_marked(self):
        store = EventStore()
        writer = RagDebugWriter(store, "run", "101")
        store.fail_once = True
        await writer.record("response", {"body": "data"})
        self.assertEqual(writer.failed_captures, 1)
        self.assertFalse(store.restore("response")["complete"])
        await writer.record("tool_end", {"status": "succeeded"})
        self.assertEqual(store.restore("tool_end")["previous_capture_failures"], 1)

    async def test_cancel_during_part_write_leaves_incomplete_manifest_and_propagates(self):
        class CancelStore(EventStore):
            async def append_run_event(self, run_id, event_type, data, *, user_id):
                if event_type == "rag_debug_part" and data["part_index"] == 1:
                    raise asyncio.CancelledError()
                await super().append_run_event(run_id, event_type, data, user_id=user_id)
        store = CancelStore()
        writer = RagDebugWriter(store, "run", "101")
        with self.assertRaises(asyncio.CancelledError):
            await writer.record("response", {"body": "部分响应" * 20000})
        self.assertFalse(store.restore("response")["complete"])
        self.assertEqual(writer.failed_captures, 1)

    async def test_parameters_raw_candidates_and_each_filter_reason_are_saved(self):
        records = [
            {"chunk_text": "同年低分保留", "fileName": "a.pdf", "score": 0.7, "tags": ["2021年", "3月"]},
            {"chunk_text": "阈值以下", "score": 0.1, "tags": ["2021年", "3月"]},
            {"chunk_text": "异年高分淘汰", "score": 0.99, "tags": ["2020年"]},
            {"chunk_text": "", "score": 1},
            {"chunk_text": "超过召回数量", "score": 1},
        ]
        backend = make_backend(FakeSession(FakeResponse({"code": 200, "data": {"records": records}})), FakeReranker())
        self.addAsyncCleanup(backend.close)
        store = EventStore()
        with rag_debug_scope(RagDebugWriter(store, "run", "101").record):
            result = await backend.search_all("2021年3月", [("knowledge-1", 3, 1)], {"tags": ["2021年", "3月"]}, score=0.65, result_score_threshold=0.3)
        self.assertEqual([item.text for item in result], ["同年低分保留"])
        self.assertEqual(store.restore("response")["data"]["body"]["data"]["records"], records)
        self.assertEqual(store.restore("request")["data"]["payload"]["score"], 0.65)
        decisions = store.restore("parse")["data"]["decisions"]
        self.assertEqual(decisions[-2]["reason"], "invalid_record_or_empty_text")
        self.assertEqual(decisions[-1]["reason"], "beyond_recall_limit")
        self.assertEqual(store.restore("sort")["data"]["sort_rule"], "time_tags_then_year_then_score")
        filtered = store.restore("filter")["data"]
        self.assertEqual(filtered["result_score_threshold"], 0.3)
        self.assertEqual([item["reason"] for item in filtered["candidates"]], ["kept", "below_score_threshold", "beyond_top_n"])
        self.assertFalse(filtered["rerank_executed"])

    async def test_retry_preserves_both_responses_and_attempt_numbers(self):
        backend = make_backend(FakeSession(FakeResponse({"error": "busy"}, 503),
            FakeResponse({"code": 200, "data": {"records": []}})), FakeReranker())
        backend.max_retries = 1
        backend.retry_backoff = 0
        self.addAsyncCleanup(backend.close)
        captured = []
        async def collect(stage, data):
            captured.append((stage, data))
        with rag_debug_scope(collect):
            await backend.search_all("query", [("knowledge-1", 2, 1)])
        requests = [data for stage, data in captured if stage == "request"]
        responses = [data for stage, data in captured if stage == "response"]
        self.assertEqual([item["attempt"] for item in requests], [1, 2])
        self.assertEqual(len({item["request_id"] for item in requests}), 1)
        self.assertEqual([item["http_status"] for item in responses], [503, 200])
        self.assertTrue(next(data for stage, data in captured if stage == "request_error")["will_retry"])

    async def test_concurrent_scopes_do_not_mix_and_reset_after_cancellation(self):
        first, second = [], []
        async def task(target, value):
            async def collect(stage, data):
                target.append(data["value"])
            with rag_debug_scope(collect):
                await asyncio.sleep(0)
                await record_rag_debug("query", value=value)
        await asyncio.gather(task(first, "first"), task(second, "second"))
        await record_rag_debug("query", value="outside")
        self.assertEqual(first, ["first"])
        self.assertEqual(second, ["second"])
        async def cancelled():
            with rag_debug_scope(lambda *args: None):
                raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled()
        await record_rag_debug("query", value="after_cancel")

    async def test_backend_cancel_and_invalid_json_leave_diagnostics(self):
        class InvalidJson(FakeResponse):
            async def json(self, **kwargs):
                raise ValueError("invalid JSON")
            async def text(self):
                return "unparseable response body"
        backend = make_backend(FakeSession(InvalidJson(None)), FakeReranker())
        self.addAsyncCleanup(backend.close)
        captured = []
        async def collect(stage, data):
            captured.append((stage, data))
        with rag_debug_scope(collect), self.assertRaises(Exception):
            await backend.search_all("query", [("knowledge-1", 2, 1)])
        response = next(data for stage, data in captured if stage == "response")
        self.assertFalse(response["valid_json"])
        self.assertEqual(response["body"], "unparseable response body")
        backend._session = FakeSession(asyncio.CancelledError())
        captured.clear()
        with rag_debug_scope(collect), self.assertRaises(asyncio.CancelledError):
            await backend.search_all("query", [("knowledge-1", 2, 1)])
        self.assertTrue(next(data for stage, data in captured if stage == "request_error")["cancelled"])

    async def test_agent_links_original_rewritten_actual_queries_and_multiple_calls(self):
        settings = get_settings().model_copy(deep=True)
        settings.business_scenarios["general"].rag.knowledge_bases = [
            RagKnowledgeBaseSettings(knowledge_id="knowledge-1", recall_top_n=2, top_n=1)]
        backend = make_backend(FakeSession(FakeResponse({"code": 200, "data": {"records": [
            {"chunk_text": "资料", "fileName": "a.pdf", "score": 0.9}]
        }})), FakeReranker())
        tools = ToolExecutor()
        tools.register("rag_search", "检索", RagSearchTool(backend).execute, contextual=True)
        agent = AgentLoop(FakeLlm([
            ([], [{"id": "first", "name": "rag_search", "args": {"query": "DO"}},
                  {"id": "second", "name": "rag_search", "args": {"query": "第二次"}}]),
            (["结论<<SRC_1>>"], []),
        ]), tools)
        self.addAsyncCleanup(agent.close)
        self.addAsyncCleanup(backend.close)
        captured = []
        async def collect(stage, data):
            captured.append((stage, data))
        with patch("services.context_service.expand_query_synonyms", side_effect=lambda value: value), patch("agents.tools.rag_search.get_settings", return_value=settings), patch(
            "agents.tools.rag_search.expand_query_synonyms", side_effect=lambda value: value + "（扩展）"):
            events = [event async for event in agent.run_stream("原始用户问题", [], rag_debug_callback=collect)]
        queries = [data for stage, data in captured if stage == "query"]
        self.assertEqual([item["tool_call_id"] for item in queries], ["first", "second"])
        self.assertEqual(queries[0]["original_question"], "原始用户问题")
        self.assertEqual(queries[0]["model_query"], "DO")
        self.assertEqual(queries[0]["actual_query"], "DO（扩展）")
        self.assertEqual(len([stage for stage, _ in captured if stage == "tool_end"]), 2)
        self.assertTrue(any(stage == "sources" for stage, _ in captured))
        self.assertFalse(any(event.type.startswith("rag_debug") for event in events))


class RagDebugDatabaseTest(DatabaseFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        store = EventStore()
        await RagDebugWriter(store, "r1", "101").record("response", {"body": "完整候选数据" * 9000, "tool_call_id": "call"})
        self.manifest = next(data for kind, data in store.events if kind == "rag_debug")
        from database.models import RunEventModel
        with self.engine.begin() as connection:
            for sequence, (kind, payload) in enumerate(store.events, start=4):
                connection.execute(self.tables[RunEventModel].insert().values(run_id="r1", sequence_no=sequence,
                    event_type=kind, payload=payload, created_at=self.now))
        self.statements.clear()

    async def test_admin_reads_manifest_then_reassembles_only_authorized_run(self):
        events = await self.service.get_events("c1", "r1", after_sequence=3, page_size=100)
        self.assertEqual(len(events.items), 1)
        self.assertEqual(events.items[0]["eventType"], "rag_debug")
        self.assertIsNone(events.items[0]["payload"]["data"])
        capture_id = self.manifest["capture_id"]
        result = await self.service.get_rag_capture("c1", "r1", capture_id)
        self.assertTrue(result.capture["complete"])
        self.assertEqual(result.capture["data"]["body"], "完整候选数据" * 9000)
        self.assertIsNone(await self.service.get_rag_capture("c2", "r1", capture_id))
        self.assertIsNone(await self.service.get_rag_capture("c1", "r2", capture_id))

    async def test_real_user_replay_excludes_debug_before_pagination(self):
        repository = MySQLConversationRepository(SimpleNamespace(session_factory=lambda: ReadSession(self.engine)))
        events, status = await repository.list_run_events("c1", "r1", after=0, user_id="101", limit=2)
        self.assertEqual([item.event for item in events], ["process", "retrieval"])
        events, _ = await repository.list_run_events("c1", "r1", after=2, user_id="101", limit=2)
        self.assertEqual([item.event for item in events], ["done"])
        events, _ = await repository.list_run_events("c1", "r1", after=3, user_id="101")
        self.assertEqual(events, [])
        foreign, status = await repository.list_run_events("c1", "r1", after=0, user_id="102")
        self.assertEqual((foreign, status), ([], None))

    async def test_capture_http_endpoint_checks_admin_and_conversation_scope(self):
        from fastapi.testclient import TestClient
        from main import app
        settings = get_settings()
        headers = {settings.internal_request.header_name: settings.internal_request.token.get_secret_value(),
                   "X-User-Id": "1", "X-Auth-Source": "dh-gateway", "X-User-Type": "ADMIN"}
        capture_id = self.manifest["capture_id"]
        url = f"/admin/conversations/c1/runs/r1/rag-debug/{capture_id}"
        client = TestClient(app)
        try:
            with patch("controller.admin_conversation_controller.service", self.service):
                result = client.get(url, headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertTrue(result.json()["data"]["capture"]["complete"])
                self.assertEqual(client.get(url, headers={**headers, "X-User-Type": "USER"}).status_code, 403)
                self.assertEqual(client.get(url.replace("/c1/", "/c2/"), headers=headers).status_code, 404)
        finally:
            client.close()
