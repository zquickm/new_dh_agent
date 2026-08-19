import unittest

from agents.events import AgentEvent
from repositories.memory_conversation_repository import conversation_repository
from schemas.chat import AgentRequest
from services.chat_service import ChatService


class FakeAgent:
    model_name = "fake-model"

    async def run_stream(self, question, history):
        del question, history
        yield AgentEvent(
            type="tool_started",
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(
            type="retrieval",
            data={
                "tool_call_id": "call-1",
                "tool_name": "rag_search",
                "query": "问题",
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
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(
            type="tool_finished",
            content="检索完成",
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(type="answer_delta", content="你好[[cite:1]]", round_number=2)
        yield AgentEvent(type="answer_delta", content="世界", round_number=2)


class ChatServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_maps_agent_events_and_persists_exchange(self):
        conversation = await conversation_repository.create("测试")
        service = ChatService()
        service._agent = FakeAgent()
        events = [
            event
            async for event in service.stream_chat(
                AgentRequest(question="问题", conversation_id=conversation.id)
            )
        ]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "conversation",
                "process",
                "retrieval",
                "process",
                "token",
                "token",
                "done",
            ],
        )
        self.assertEqual(events[2]["data"]["chunks"][0]["preview"], "检索片段")
        self.assertEqual(events[3]["data"]["status"], "success")
        self.assertEqual(events[1]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["details"]["result_count"], 1)
        self.assertEqual(events[1]["data"]["agent_id"], "agent-loop")
        self.assertEqual(events[-1]["data"]["answer"], "你好[[cite:1]]世界")
        self.assertEqual(events[-1]["data"]["sources"][0]["citation_id"], 1)
        detail = await conversation_repository.get(conversation.id)
        self.assertEqual([message.role for message in detail.messages], ["user", "assistant"])
        self.assertEqual(detail.messages[-1].sources[0]["preview"], "检索片段")
