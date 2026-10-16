import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agents.events import AgentEvent
from config.settings import ExactQaSettings, InternalRequestSettings
from services.exact_qa_service import ExactQaService
from tests import test_exact_qa as exact_qa_tests

snapshot = exact_qa_tests.snapshot


class QaModelMatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(exact_qa=ExactQaSettings(enabled=True),
            dictionaries=SimpleNamespace(base_url="http://knowledge", connect_timeout=1, verify_ssl=True),
            internal_request=InternalRequestSettings(token="unit-test-secret-token"))
        self.candidates = [
            {"qaId": 1, "versionId": 2, "question": snapshot().question},
            {"qaId": 3, "versionId": 4, "question": "另一个候选问题"},
        ]
        self.question = "原问题的不同表述"
        self.llm = SimpleNamespace(invoke=AsyncMock(return_value=(
            '{"status":"matched","candidate_id":"qa-1-v2"}', [])))
        self.service = ExactQaService(llm=self.llm)
        self.requests = []
        self.snapshot_data = snapshot().model_dump(by_alias=True)

    def handler(self, request):
        self.requests.append(request)
        self.assertEqual(request.headers["X-DH-Internal-Token"], "unit-test-secret-token")
        self.assertEqual(request.method, "GET")
        if request.url.path == "/internal/v1/qa/candidates":
            return httpx.Response(200, json={"code": 200, "data": self.candidates})
        self.assertEqual(request.url.path, "/internal/v1/qa/1/versions/2")
        return httpx.Response(200, json={"code": 200, "data": self.snapshot_data})

    async def match(self, handler=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler or self.handler))
        with patch("services.exact_qa_service.get_settings", return_value=self.settings), \
             patch.object(self.service, "_http_client", return_value=client):
            return await self.service.match(self.question)

    async def test_all_candidate_questions_are_sent_once_and_answer_is_unchanged(self):
        result = await self.match()
        self.assertEqual(result.model_dump(), snapshot().model_dump())
        self.llm.invoke.assert_awaited_once()
        args = self.llm.invoke.await_args
        self.assertEqual(args.kwargs, {"temperature": 0})
        messages = args.args[0]
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        self.assertEqual(json.loads(messages[1]["content"]), {
            "user_question": self.question, "candidates": [
                {"candidate_id": "qa-1-v2", "question": self.candidates[0]["question"]},
                {"candidate_id": "qa-3-v4", "question": self.candidates[1]["question"]},
            ]})
        self.assertNotIn(snapshot().answer, messages[1]["content"])
        self.assertEqual(len(self.requests), 2)

    async def test_empty_catalog_does_not_call_model(self):
        self.candidates = []
        self.assertIsNone(await self.match())
        self.llm.invoke.assert_not_awaited()
        self.assertEqual(len(self.requests), 1)

    async def test_no_match_and_ambiguity_do_not_fetch_answers(self):
        for status in ("no_match", "ambiguous"):
            self.requests.clear()
            self.llm.invoke.return_value = (json.dumps({"status": status, "candidate_id": None}), [])
            self.assertIsNone(await self.match())
            self.assertEqual(len(self.requests), 1)

    async def test_invalid_model_results_fall_back_without_fetching_arbitrary_ids(self):
        for content, calls in [
            ("not-json", []), ('{"status":"matched","candidate_id":"qa-999-v1"}', []),
            ('{"status":"matched","candidate_id":null}', []),
            ('{"status":"no_match","candidate_id":"qa-1-v2"}', []),
            ('{"status":"matched","candidate_id":"qa-1-v2","answer":"invented"}', []),
            ('{"status":"matched","candidate_id":"qa-1-v2"}', [{"name": "tool"}]),
        ]:
            with self.subTest(content=content):
                self.requests.clear()
                self.llm.invoke.return_value = (content, calls)
                self.assertIsNone(await self.match())
                self.assertEqual(len(self.requests), 1)

    async def test_model_failure_and_timeout_fall_back(self):
        self.llm.invoke.side_effect = RuntimeError("unavailable")
        self.assertIsNone(await self.match())

        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()

        self.llm.invoke.side_effect = blocked
        self.settings.exact_qa.match_timeout = 0.01
        self.assertIsNone(await self.match())

    async def test_candidate_service_failure_falls_back_without_model(self):
        self.assertIsNone(await self.match(lambda request: httpx.Response(503)))
        self.llm.invoke.assert_not_awaited()

    async def test_disabled_deleted_or_superseded_snapshot_is_a_miss(self):
        self.snapshot_data = None
        self.assertIsNone(await self.match())

    async def test_wrong_snapshot_identity_is_never_returned(self):
        for field, value in (("qaId", 9), ("versionId", 9), ("question", "被替换的问题")):
            with self.subTest(field=field):
                self.snapshot_data = {**snapshot().model_dump(by_alias=True), field: value}
                self.assertIsNone(await self.match())

    async def test_duplicate_or_malformed_candidates_fail_before_model(self):
        for candidates in (None, [self.candidates[0], self.candidates[0]], [{"qaId": 1}]):
            self.candidates = candidates
            self.assertIsNone(await self.match())
        self.llm.invoke.assert_not_awaited()

    async def test_disabled_matching_does_not_call_knowledge_or_model(self):
        self.settings.exact_qa.enabled = False
        self.assertIsNone(await self.match())
        self.assertEqual(self.requests, [])
        self.llm.invoke.assert_not_awaited()


class QaModelRoutingTest(unittest.IsolatedAsyncioTestCase):
    setUp = exact_qa_tests.ExactQaChatTest.setUp
    events = exact_qa_tests.ExactQaChatTest.events

    async def test_semantic_match_uses_original_user_question_and_fixed_snapshot(self):
        llm = SimpleNamespace(invoke=AsyncMock(return_value=(
            '{"status":"matched","candidate_id":"qa-1-v2"}', [])))
        service = ExactQaService(llm=llm)
        candidates = [{"qaId": 1, "versionId": 2, "question": snapshot().question}]
        settings = SimpleNamespace(exact_qa=ExactQaSettings(enabled=True),
            dictionaries=SimpleNamespace(base_url="http://knowledge", connect_timeout=1, verify_ssl=True),
            internal_request=InternalRequestSettings(token="unit-test-secret-token"))

        def handler(request):
            data = candidates if request.url.path.endswith("/candidates") else snapshot().model_dump(by_alias=True)
            return httpx.Response(200, json={"code": 200, "data": data})

        self.service._exact_qa_service = service
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("services.exact_qa_service.get_settings", return_value=settings), \
             patch.object(service, "_http_client", return_value=client):
            events = await self.events(question="原问题的另一种问法")
        done = events[-1]["data"]
        self.assertEqual(done["answer_origin"], "exact_qa")
        self.assertEqual(done["answer"], snapshot().answer)
        self.assertEqual(done["reasoning"], snapshot().reasoning.model_dump())
        self.assertEqual(done["sources"], snapshot().sources)
        self.assertIsNone(self.service._agent)
        saved = await self.repository.get(done["conversation_id"], user_id="101")
        self.assertEqual(saved.messages[0].content, "原问题的另一种问法")

    async def test_invalid_model_result_continues_normal_chat(self):
        class Agent:
            model_name = "normal-model"

            async def run_stream(self, question, history, run_id="-"):
                yield AgentEvent(type="answer_delta", content="正常模型回答", round_number=1)

        self.service._agent = Agent()
        service = ExactQaService(llm=SimpleNamespace(invoke=AsyncMock(return_value=("invalid-json", []))))
        self.service._exact_qa_service = service
        settings = SimpleNamespace(exact_qa=ExactQaSettings(enabled=True),
            dictionaries=SimpleNamespace(base_url="http://knowledge", connect_timeout=1, verify_ssl=True),
            internal_request=InternalRequestSettings(token="unit-test-secret-token"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"code": 200, "data": [{"qaId": 1, "versionId": 2, "question": "候选问题"}]})))
        with patch("services.exact_qa_service.get_settings", return_value=settings), \
             patch.object(service, "_http_client", return_value=client):
            events = await self.events(question="用户问题")
        done = next(event["data"] for event in events if event["event"] == "done")
        self.assertEqual(done["answer_origin"], "model")
        self.assertEqual(done["answer"], "正常模型回答")
