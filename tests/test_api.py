import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from agents.events import AgentEvent
from main import app
from services.chat_service import chat_service
from services.sensitive_word_service import SensitiveWordMatcher
from services.request_context_adapter import enrich_question_with_frontend_context
from tests.fakes.memory_conversation_repository import MemoryConversationRepository


INTERNAL_HEADER = "X-DH-Internal-Token"
INTERNAL_TOKEN = "dh-internal-token-change-in-production"


class FakeAgent:
    model_name = "fake-model"

    def __init__(self):
        self.last_question = None

    async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
        del run_id
        self.last_question = enrich_question_with_frontend_context(
            question, frontend_context
        )
        del history
        yield AgentEvent(
            type="retrieval",
            data={
                "tool_call_id": "call-rag",
                "tool_name": "rag_search",
                "query": "你好",
                "result_count": 1,
                "chunks": [
                    {
                        "rank": 1,
                        "source": "资料.pdf",
                        "preview": "检索片段",
                        "page_index": 0,
                    }
                ],
            },
            sources=[
                {
                    "citation_id": 1,
                    "source": "资料.pdf",
                    "line": None,
                    "score": 0.9,
                    "preview": "检索片段",
                    "title": "资料",
                    "url": None,
                    "page_index": 0,
                    "chunk_id": "chunk-1",
                    "knowledge_id": "kb-1",
                }
            ],
            tool_name="rag_search",
            tool_call_id="call-rag",
        )
        yield AgentEvent(
            type="answer_delta",
            content="流式回答[[cite:1]]",
            round_number=1,
        )


class ApiTest(unittest.TestCase):
    def setUp(self):
        recommendations = patch("services.chat_service.generate_recommendations", return_value=[])
        recommendations.start()
        self.addCleanup(recommendations.stop)
        # Python 3.9 中，前一组异步测试可能已关闭同步测试桩所需的默认循环。
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.addCleanup(asyncio.set_event_loop, None)
            self.addCleanup(loop.close)
        repository = MemoryConversationRepository()
        for target in (
            "services.conversation_service.conversation_repository",
            "services.chat_service.conversation_repository",
        ):
            repository_patch = patch(target, repository)
            repository_patch.start()
            self.addCleanup(repository_patch.stop)
        synonym_patch = patch(
            "services.context_service.expand_query_synonyms",
            side_effect=lambda value: value,
        )
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)
        self.previous_agent = chat_service._agent
        self.previous_matcher = chat_service._sensitive_word_matcher
        self.fake_agent = FakeAgent()
        chat_service._agent = self.fake_agent
        chat_service._sensitive_word_matcher = SensitiveWordMatcher(
            [{"word": "测试敏感词一", "applyScope": "CHAT_INPUT"}]
        )
        self.client = TestClient(app)
        self.client.headers[INTERNAL_HEADER] = INTERNAL_TOKEN
        self.client.headers["X-User-Id"] = "101"
        self.client.headers["X-Username"] = "test-user"
        self.client.headers["X-Auth-Source"] = "dh-gateway"

    def tearDown(self):
        chat_service._agent = self.previous_agent
        chat_service._sensitive_word_matcher = self.previous_matcher

    def test_frontend_conversation_and_sse_contract(self):
        created = self.client.post("/conversations", json={"title": "测试"})
        self.assertEqual(created.status_code, 200)
        conversation_id = created.json()["data"]["id"]

        with self.client.stream(
            "POST",
            "/agent/chat/stream",
            json={
                "question": "你好",
                "conversation_id": conversation_id,
                "chat_history": [],
            },
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertIn("event: conversation", body)
        self.assertIn("event: retrieval", body)
        self.assertIn("event: token", body)
        self.assertIn("event: done", body)
        self.assertIn("检索片段", body)
        self.assertIn("流式回答[[cite:1]]", body)

        detail = self.client.get(f"/conversations/{conversation_id}").json()["data"]
        self.assertEqual(
            [message["role"] for message in detail["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual(detail["messages"][-1]["sources"][0]["citation_id"], 1)
        run_id = detail["messages"][-1]["run_id"]
        run_response = self.client.get(
            f"/conversations/{conversation_id}/runs/{run_id}"
        )
        self.assertEqual(run_response.status_code, 200)
        self.assertEqual(run_response.json()["data"]["status"], "completed")
        self.assertEqual(run_response.json()["data"]["model"], "fake-model")
        conversation_page = self.client.get("/conversations").json()["data"]
        listed = next(
            item for item in conversation_page["items"]
            if item["id"] == conversation_id
        )
        self.assertIs(listed["is_generating"], False)
        with self.client.stream(
            "GET",
            f"/conversations/{conversation_id}/runs/{run_id}/events?after=1",
            headers={"Last-Event-ID": "2"},
        ) as replay_response:
            replay_body = "".join(replay_response.iter_text())
        self.assertEqual(replay_response.status_code, 200)
        replay_blocks = [
            {
                key.strip(): value.strip()
                for line in block.splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            for block in replay_body.strip().split("\n\n")
        ]
        self.assertEqual(
            [block["event"] for block in replay_blocks], ["token", "done"]
        )
        self.assertEqual([int(block["id"]) for block in replay_blocks], [3, 4])
        tool_calls_response = self.client.get(
            f"/conversations/{conversation_id}/runs/{run_id}/tool-calls"
        )
        self.assertEqual(tool_calls_response.status_code, 200)
        self.assertEqual(tool_calls_response.json()["data"], [])
        missing_tool_call = self.client.get(
            f"/conversations/{conversation_id}/runs/{run_id}/tool-calls/missing"
        )
        self.assertEqual(missing_tool_call.status_code, 404)

    def test_sensitive_input_uses_normal_sse_contract_without_calling_agent(self):
        with self.client.stream(
            "POST",
            "/agent/chat/stream",
            json={"question": "这里包含测试敏感词一"},
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        frames = []
        for block in body.strip().split("\n\n"):
            lines = block.splitlines()
            fields = {
                key.strip(): value.strip()
                for line in lines
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            frames.append(
                {
                    "event": fields["event"],
                    "id": int(fields["id"]),
                    "payload": json.loads(fields["data"]),
                }
            )
        self.assertEqual(
            [frame["event"] for frame in frames],
            ["conversation", "token", "done"],
        )
        self.assertEqual([frame["id"] for frame in frames], [1, 2, 3])
        conversation = frames[0]["payload"]["data"]
        self.assertEqual(
            frames[1]["payload"]["data"],
            {"content": "您的输入包含敏感词，请修改后重新提问。"},
        )
        self.assertEqual(
            frames[2]["payload"]["data"],
            {
                "conversation_id": conversation["conversation_id"],
                "run_id": conversation["run_id"],
                "answer": "您的输入包含敏感词，请修改后重新提问。",
                "model": "sensitive-word-filter",
                "sources": [],
                "images": [],
                "assistant_message_id": frames[2]["payload"]["data"][
                    "assistant_message_id"
                ],
            },
        )
        self.assertIsNotNone(
            frames[2]["payload"]["data"]["assistant_message_id"]
        )
        self.assertIsNone(self.fake_agent.last_question)
        detail = self.client.get(
            f"/conversations/{conversation['conversation_id']}"
        ).json()["data"]
        self.assertEqual(detail["message_count"], 2)
        self.assertEqual(
            [message["role"] for message in detail["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual(detail["messages"][-1]["status"], "failed")
        run = self.client.get(
            f"/conversations/{conversation['conversation_id']}/runs/"
            f"{conversation['run_id']}"
        ).json()["data"]
        self.assertEqual(run["status"], "rejected")
        self.assertEqual(run["error_code"], "SENSITIVE_INPUT_REJECTED")

    def test_run_event_replay_is_user_isolated(self):
        with self.client.stream(
            "POST", "/agent/chat/stream", json={"question": "你好"}
        ) as response:
            body = "".join(response.iter_text())
        conversation_frame = next(
            block for block in body.split("\n\n") if block.startswith("event: conversation")
        )
        data_line = next(
            line for line in conversation_frame.splitlines() if line.startswith("data:")
        )
        conversation = json.loads(data_line.removeprefix("data: "))["data"]

        self.client.headers["X-User-Id"] = "202"
        replay = self.client.get(
            f"/conversations/{conversation['conversation_id']}/runs/"
            f"{conversation['run_id']}/events"
        )
        self.assertEqual(replay.status_code, 404)

    def test_run_event_replay_rejects_invalid_cursor(self):
        for value in ("invalid", "-1"):
            with self.subTest(value=value):
                response = self.client.get(
                    "/conversations/missing/runs/missing/events",
                    headers={"Last-Event-ID": value},
                )
                self.assertEqual(response.status_code, 400)

    def test_rejects_request_without_gateway_internal_header(self):
        with patch(
            "main.dictionary_refresh_service.start", new=AsyncMock()
        ), patch(
            "main.dictionary_refresh_service.stop", new=AsyncMock()
        ), patch(
            "main.database_runtime.start", new=AsyncMock()
        ), patch(
            "main.system_prompt_service.start", new=AsyncMock()
        ), patch(
            "main.skill_service.start", new=AsyncMock()
        ), patch("main.database_runtime.close", new=AsyncMock()):
            with TestClient(app) as direct_client:
                response = direct_client.get("/health")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)

    def test_stream_accepts_esic_map_context_without_leaking_it_to_history(self):
        created = self.client.post("/conversations", json={"title": "地图选区测试"})
        conversation_id = created.json()["data"]["id"]
        question = "这个区域有哪些观测站点"

        with self.client.stream(
            "POST",
            "/agent/chat/stream",
            json={
                "question": question,
                "conversation_id": conversation_id,
                "map_context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [121.64807177848331, 31.145291852528445],
                            [122.51079620689507, 31.145291852528445],
                            [122.51079620689507, 31.926311797831147],
                            [121.64807177848331, 31.926311797831147],
                            [121.64807177848331, 31.145291852528445],
                        ]],
                        "bbox": [
                            121.64807177848331,
                            31.145291852528445,
                            122.51079620689507,
                            31.926311797831147,
                        ],
                    }
                },
            },
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", body)
        self.assertIn('"query_type":"polygon"', self.fake_agent.last_question)
        self.assertIn('"crs":"EPSG:4490"', self.fake_agent.last_question)

        detail = self.client.get(f"/conversations/{conversation_id}").json()["data"]
        self.assertEqual(detail["messages"][0]["content"], question)

    def test_stream_accepts_circle_map_context(self):
        with self.client.stream(
            "POST",
            "/agent/chat/stream",
            json={
                "question": "这个圆形区域有哪些观测站点",
                "chat_history": [
                    {"role": "user", "content": "先看前一个区域"},
                    {"role": "assistant", "content": "已显示前一个区域"},
                    {
                        "event": "CIRCLE_DRAW",
                        "payload": {
                            "center": [121.8, 30.4],
                            "radius": 25000,
                        },
                    },
                ],
                "map_context": {
                    "drawn_geometry": {
                        "center": [121.8, 30.4],
                        "radius": 25000,
                    }
                },
            },
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", body)
        self.assertIn('"query_type":"circle"', self.fake_agent.last_question)
        self.assertIn('"center":[121.8,30.4]', self.fake_agent.last_question)
        self.assertIn('"radius":25000.0', self.fake_agent.last_question)

    def test_stream_restores_polygonized_circle_with_radius_metadata(self):
        with self.client.stream(
            "POST",
            "/agent/chat/stream",
            json={
                "sessionId": "1787889840431",
                "message": "这个区域有哪些站点",
                "context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [121.45, 31.93],
                            [122.32, 31.18],
                            [121.45, 30.44],
                            [120.58, 31.18],
                            [121.45, 31.93],
                        ]],
                        "bbox": [121.45, 30.83, 122.22, 31.18],
                        "radius": 82991.07555434913,
                    }
                },
            },
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", body)
        self.assertIn('"query_type":"circle"', self.fake_agent.last_question)
        self.assertIn('"center":[', self.fake_agent.last_question)
        self.assertIn('"radius":82991.07555434913', self.fake_agent.last_question)
        self.assertNotIn('"polygon"', self.fake_agent.last_question)

    def test_validation_error_returns_concise_field_path(self):
        response = self.client.post(
            "/agent/chat/stream",
            json={
                "question": "输出观测站点",
                "map_context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [[[181, 30], [182, 30], [182, 31], [181, 30]]],
                        "bbox": [181, 30, 182, 31],
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 422)
        message = response.json()["message"]
        self.assertIn("body.map_context.drawn_geometry", message)
        self.assertNotIn("'input'", message)

    def test_accepts_area_query_after_fly_to_transient_state(self):
        with self.client.stream(
            "POST",
            "/agent/chat/stream",
            json={
                "question": "该区域有哪些观测站点",
                "chat_history": [
                    {"role": "user", "content": "查询小洋山站水温"},
                    {"role": "assistant", "content": "已查询小洋山站"},
                    {"role": "assistant", "content": ""},
                    {"role": "tool", "content": "FLY_TO"},
                    {"event": "map_action", "content": "FLY_TO"},
                ],
                "map_context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [121.6, 31.1],
                            [122.5, 31.1],
                            [122.5, 31.9],
                            [121.6, 31.9],
                        ]],
                        "bbox": [0, 0, 0, 0],
                    }
                },
            },
        ) as response:
            body = "".join(response.iter_text())

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", body)
