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
            type="tool_finished",
            content="检索完成",
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(type="answer_delta", content="你好", round_number=2)
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
            ["conversation", "process", "process", "token", "token", "done"],
        )
        self.assertEqual(events[2]["data"]["status"], "success")
        self.assertEqual(events[1]["data"]["step_id"], "call-1")
        self.assertEqual(events[2]["data"]["step_id"], "call-1")
        self.assertEqual(events[1]["data"]["agent_id"], "agent-loop")
        self.assertEqual(events[-1]["data"]["answer"], "你好世界")
        detail = await conversation_repository.get(conversation.id)
        self.assertEqual([message.role for message in detail.messages], ["user", "assistant"])
