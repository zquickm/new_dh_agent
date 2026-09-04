import json
import unittest

from fastapi.testclient import TestClient

from agents.events import AgentEvent
from main import app
from services.chat_service import chat_service


INTERNAL_HEADER = "X-DH-Internal-Token"
INTERNAL_TOKEN = "dh-internal-token-change-in-production"


class FakeAgent:
    model_name = "fake-model"

    def __init__(self):
        self.last_question = None

    async def run_stream(self, question, history, run_id="-"):
        del run_id
        self.last_question = question
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
        self.previous_agent = chat_service._agent
        self.fake_agent = FakeAgent()
        chat_service._agent = self.fake_agent
        self.client = TestClient(app)
        self.client.headers[INTERNAL_HEADER] = INTERNAL_TOKEN

    def tearDown(self):
        chat_service._agent = self.previous_agent

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
            frames.append(
                {
                    "event": lines[0].removeprefix("event: "),
                    "payload": json.loads(lines[1].removeprefix("data: ")),
                }
            )
        self.assertEqual(
            [frame["event"] for frame in frames],
            ["conversation", "token", "done"],
        )
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
                "assistant_message_id": None,
            },
        )
        self.assertIsNone(self.fake_agent.last_question)
        detail = self.client.get(
            f"/conversations/{conversation['conversation_id']}"
        ).json()["data"]
        self.assertEqual(detail["message_count"], 0)
        self.assertEqual(detail["messages"], [])

    def test_rejects_request_without_gateway_internal_header(self):
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
