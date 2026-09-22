import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.events import AgentEvent
from schemas.chat import AgentRequest
from schemas.conversation import ConversationDetail, ConversationMessage
from services.chat_service import ChatService
from services.recommendation_service import generate_recommendations
from services.token_budget_service import ContextBudgetExceeded
from tests.fakes.memory_conversation_repository import MemoryConversationRepository


QUESTIONS = ["这些站点如何分布？", "有哪些观测要素？", "这些站点如何分类？"]


class Counter:
    def count_request(self, messages, tools):
        return sum(len(item["content"]) for item in messages)


class RecommendationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = SimpleNamespace(
            token_budget=SimpleNamespace(counter=Counter()),
            context_window_tokens=10000, max_output_tokens=100, token_safety_margin=10,
            llm=SimpleNamespace(invoke=AsyncMock(return_value=(json.dumps(QUESTIONS), []))),
        )
        self.conversation = ConversationDetail(id="c1", title="测试", created_at="", updated_at="")
        for index in range(4):
            self.conversation.messages.extend([
                ConversationMessage(id=f"u{index}", role="user", content=f"问题{index}", created_at=""),
                ConversationMessage(
                    id=f"a{index}", role="assistant", content=f"回答{index}[[cite:1]]", created_at="",
                    model_messages=[{"role": "tool", "content": "不能进入推荐的内部工具记录"}],
                ),
            ])
        self.conversation.messages.extend([
            ConversationMessage(id="failed-user", role="user", content="失败的问题", created_at=""),
            ConversationMessage(id="failed-answer", role="assistant", content="失败的回答", status="failed", created_at=""),
        ])

    async def generate(self):
        return await generate_recommendations(self.agent, self.conversation, "当前问题", "当前回答[[cite:1]]", "a-current")

    async def test_recent_visible_rounds_and_current_answer_only(self):
        result = await self.generate()
        messages = self.agent.llm.invoke.call_args.args[0]
        self.assertEqual([item["content"] for item in messages[1:-1]], [
            "问题1", "回答1", "问题2", "回答2", "问题3", "回答3", "当前问题", "当前回答",
        ])
        self.assertTrue(all(set(item) == {"role", "content"} for item in messages))
        self.assertNotIn("tools", self.agent.llm.invoke.call_args.kwargs)
        self.assertEqual([item["question"] for item in result], QUESTIONS)
        self.assertTrue(all(item["message_id"] == "a-current" and item["source_type"] == 2 for item in result))
        self.assertTrue(all(isinstance(item["id"], int) and item["id"] < 2**53 for item in result))
        self.assertEqual(self.conversation.messages[-1].recommend_questions, [])

    async def test_context_switch_and_message_limit(self):
        for enabled, limit, expected in [(False, 6, []), (True, 2, ["问题3", "回答3"]), (True, 1, [])]:
            self.conversation.context_enabled = enabled
            self.conversation.context_message_limit = limit
            await self.generate()
            messages = self.agent.llm.invoke.call_args.args[0]
            self.assertEqual([item["content"] for item in messages[1:-3]], expected)
            self.assertEqual(messages[-3]["content"], "当前问题")
            self.assertEqual(messages[-2]["content"], "当前回答")

    async def test_budget_drops_whole_oldest_round_and_protects_current_exchange(self):
        await self.generate()
        messages = self.agent.llm.invoke.call_args.args[0]
        total = self.agent.token_budget.counter.count_request(messages, None)
        self.conversation.context_token_limit = total - len("问题1回答1") + 110
        await self.generate()
        messages = self.agent.llm.invoke.call_args.args[0]
        self.assertEqual([item["content"] for item in messages[1:-3]], ["问题2", "回答2", "问题3", "回答3"])
        self.conversation.context_token_limit = 111
        self.agent.llm.invoke.reset_mock()
        with self.assertRaises(ContextBudgetExceeded):
            await self.generate()
        self.agent.llm.invoke.assert_not_awaited()

    async def test_invalid_output_fails_without_retry(self):
        for content in ["not json", "[]", '[1, "a", "b"]', '["", "a", "b"]',
                        '["a?", "a？", "b"]', '["当前问题？", "a", "b"]',
                        '["问题3", "a", "b"]', json.dumps(["x" * 501, "a", "b"])]:
            self.agent.llm.invoke.reset_mock()
            self.agent.llm.invoke.return_value = (content, [])
            with self.subTest(content=content), self.assertRaises(ValueError):
                await self.generate()
            self.agent.llm.invoke.assert_awaited_once()

    async def test_live_event_after_done_is_not_persisted_and_shutdown_keeps_answer(self):
        repo = MemoryConversationRepository()
        release = asyncio.Event()
        started = asyncio.Event()

        async def invoke(*args, **kwargs):
            started.set()
            await release.wait()
            return json.dumps(QUESTIONS), []

        async def run_stream(*args, **kwargs):
            yield AgentEvent(type="answer_delta", content="最终回答")

        self.agent.model_name = "fake"
        self.agent.run_stream = run_stream
        self.agent.llm.invoke.side_effect = invoke
        service = ChatService()
        service._agent = self.agent
        self.addAsyncCleanup(service.close)
        with patch("services.chat_service.conversation_repository", repo):
            for shutdown in [False, True]:
                release.clear()
                started.clear()
                stream = service.stream_chat(AgentRequest(question="当前问题"), user_id="101")
                async for event in stream:
                    if event["event"] == "done":
                        done = event
                        break
                await asyncio.wait_for(started.wait(), 1)
                if shutdown:
                    await service.close()
                    await stream.aclose()
                else:
                    release.set()
                    remaining = [event async for event in stream]
                    self.assertEqual([item["event"] for item in remaining], ["recommendations"])
                    self.assertNotIn("id", remaining[0])
                    self.assertEqual([item["question"] for item in remaining[0]["data"]["questions"]], QUESTIONS)
                data = done["data"]
                detail = await repo.get(data["conversation_id"], user_id="101")
                self.assertEqual(detail.messages[-1].status, "completed")
                self.assertEqual(detail.messages[-1].recommend_questions, [])
                replayed, status = await repo.list_run_events(data["conversation_id"], data["run_id"], after=done["id"], user_id="101")
                self.assertEqual(replayed, [])
                self.assertEqual(status, "completed")

    async def test_generation_error_returns_empty_event_without_failing_answer(self):
        repo = MemoryConversationRepository()

        async def run_stream(*args, **kwargs):
            yield AgentEvent(type="answer_delta", content="最终回答")

        service = ChatService()
        service._agent = SimpleNamespace(model_name="fake", run_stream=run_stream)
        self.addAsyncCleanup(service.close)
        for failure in [ValueError("invalid"), asyncio.TimeoutError()]:
            with patch("services.chat_service.conversation_repository", repo), patch(
                "services.chat_service.generate_recommendations", side_effect=failure
            ), self.assertLogs("services.chat_service", level="ERROR"):
                events = [event async for event in service.stream_chat(AgentRequest(question="问题"), user_id="101")]
            self.assertEqual([event["event"] for event in events][-2:], ["done", "recommendations"])
            self.assertEqual(events[-1]["data"]["questions"], [])
            detail = await repo.get(events[-1]["data"]["conversation_id"], user_id="101")
            self.assertEqual(detail.messages[-1].status, "completed")
