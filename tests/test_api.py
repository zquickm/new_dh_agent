import unittest

from fastapi.testclient import TestClient

from agents.events import AgentEvent
from main import app
from services.chat_service import chat_service


class FakeAgent:
    model_name = "fake-model"

    async def run_stream(self, question, history):
        del question, history
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
            tool_name="rag_search",
            tool_call_id="call-rag",
        )
        yield AgentEvent(type="answer_delta", content="流式回答", round_number=1)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.previous_agent = chat_service._agent
        chat_service._agent = FakeAgent()
        self.client = TestClient(app)

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
        self.assertIn("event: conversation", body)
        self.assertIn("event: retrieval", body)
        self.assertIn("event: token", body)
        self.assertIn("event: done", body)
        self.assertIn("检索片段", body)
        self.assertIn("流式回答", body)

        detail = self.client.get(f"/conversations/{conversation_id}").json()["data"]
        self.assertEqual(
            [message["role"] for message in detail["messages"]],
            ["user", "assistant"],
        )
