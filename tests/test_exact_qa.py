import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from config.settings import InternalRequestSettings, ExactQaSettings
from langchain_core.messages import AIMessageChunk

from agents.events import AgentEvent
from agents.agent_loop import AgentLoop
from agents.llm_client import HelloAgentsLLM
from agents.llm_diagnostics import DiagnosticChatOpenAI
from agents.tools.executor import ToolExecutor
from schemas.chat import AgentRequest
from schemas.reasoning import ReasoningSnapshot, apply_reasoning_event, empty_reasoning
from services.exact_qa_service import ExactQaService, ExactQaSnapshot
from services.chat_service import ChatService
from services.sensitive_word_service import SensitiveWordMatcher
from tests.fakes.memory_conversation_repository import MemoryConversationRepository


def snapshot():
    return ExactQaSnapshot.model_validate({
        "qaId": 1, "versionId": 2, "versionNo": 1, "checksum": "a" * 64,
        "question": " 原问题？\n", "answer": " 原正文🌊\r\n[[cite:7]]  ",
        "reasoning": {"schema_version": 1, "blocks": [
            {"id": "r1", "type": "reasoning", "round_number": 1,
             "status": "completed", "content": " 原思考\n🌊  "},
            {"id": "p1", "type": "process", "round_number": 1, "data": {
                "step_id": "p1", "title": "检索", "summary": " 原摘要 ",
                "status": "success", "sequence": 2, "details": {"result_count": 1},
            }},
            {"id": "r2", "type": "reasoning", "round_number": 2,
             "status": "completed", "content": " 第二轮\n "},
        ]},
        "sources": [{"citation_id": 7, "source": " 资料.pdf ", "preview": " 原文\n ", "page_index": 0}],
    })


class ExactQaChatTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repository = MemoryConversationRepository()
        repo_patch = patch("services.chat_service.conversation_repository", self.repository)
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        recommendations = patch("services.chat_service.generate_recommendations", new=AsyncMock(return_value=[]))
        recommendations.start()
        self.addCleanup(recommendations.stop)
        self.matcher = SimpleNamespace(match=AsyncMock(return_value=snapshot()))
        self.service = ChatService(exact_qa_service=self.matcher)
        self.addAsyncCleanup(self.service.close)

    async def events(self, question=None, **kwargs):
        return [e async for e in self.service.stream_chat(
            AgentRequest(question=question or snapshot().question, **kwargs), user_id="101")]

    async def test_hit_bypasses_model_and_prompt_and_preserves_every_part(self):
        self.service._prompt_provider = SimpleNamespace(active_snapshot=AsyncMock(side_effect=AssertionError("no prompt")))
        events = await self.events()
        done = events[-1]["data"]
        self.assertEqual("".join(e["data"]["content"] for e in events if e["event"] == "token"), snapshot().answer)
        self.assertEqual(done["answer"], snapshot().answer)
        self.assertEqual(done["reasoning"], snapshot().reasoning.model_dump())
        self.assertEqual(done["sources"], snapshot().sources)
        self.assertIsNone(self.service._agent)
        self.matcher.match.assert_awaited_once_with(snapshot().question)
        saved = await self.repository.get(done["conversation_id"], user_id="101")
        assistant = saved.messages[-1]
        self.assertEqual(assistant.content, snapshot().answer)
        self.assertEqual(assistant.reasoning.model_dump(), snapshot().reasoning.model_dump())
        self.assertEqual(assistant.answer_origin, "exact_qa")
        self.assertEqual(assistant.exact_qa, snapshot().provenance())
        self.assertGreater(assistant.reasoning_event_id, 0)
        # Emulate the old frontend: ignore new events, overwrite each process
        # step with its latest cumulative summary and append token content.
        steps = {}
        for event in events:
            if event["event"] == "process":
                data = event["data"]
                steps[(data.get("agent_id"), data["step_id"])] = data
        self.assertEqual(steps[("reasoning", "reasoning:r1")]["summary"], " 原思考\n🌊  ")
        self.assertEqual(steps[("reasoning", "reasoning:r2")]["status"], "success")
        self.assertEqual([step[1] for step in steps], ["reasoning:r1", "p1", "reasoning:r2"])

    async def test_saved_events_replay_same_answer_and_reasoning(self):
        events = await self.events()
        done = events[-1]["data"]
        replay, status = await self.repository.list_run_events(done["conversation_id"], done["run_id"],
            after=0, user_id="101")
        self.assertEqual(status, "completed")
        self.assertEqual([e.id for e in replay], sorted(e.id for e in replay))
        rebuilt = empty_reasoning()
        for event in replay:
            if event.event in {"reasoning_delta", "reasoning_block", "process"}:
                rebuilt = apply_reasoning_event(rebuilt, event.event, event.data)
        self.assertEqual(rebuilt, snapshot().reasoning.model_dump())
        self.assertEqual("".join(e.data["content"] for e in replay if e.event == "token"), snapshot().answer)
        foreign, _ = await self.repository.list_run_events(done["conversation_id"], done["run_id"], after=0, user_id="102")
        self.assertEqual(foreign, [])

    async def test_disconnect_does_not_cancel_fixed_answer(self):
        stream = self.service.stream_chat(AgentRequest(question=snapshot().question), user_id="101")
        first = await stream.__anext__()
        tasks = list(self.service._background_tasks.values())
        await stream.aclose()
        await asyncio.gather(*tasks)
        run = await self.repository.get_run(first["data"]["conversation_id"], first["data"]["run_id"], user_id="101")
        self.assertEqual(run.status, "completed")

    async def test_sensitive_input_does_not_query_fixed_qa(self):
        self.service._sensitive_word_matcher = SensitiveWordMatcher([{"word": "原问题", "applyScope": "CHAT_INPUT"}])
        events = await self.events()
        self.matcher.match.assert_not_awaited()
        self.assertEqual(events[-1]["data"]["model"], "sensitive-word-filter")

    async def test_foreign_conversation_does_not_query_fixed_qa(self):
        conversation = await self.repository.create("other", user_id="102")
        events = await self.events(conversation_id=conversation.id)
        self.assertEqual(events[-1]["event"], "error")
        self.matcher.match.assert_not_awaited()

    async def test_lookup_failure_is_persisted_and_does_not_run_model(self):
        self.matcher.match.side_effect = TimeoutError()
        events = await self.events()
        self.assertEqual(events[-1]["data"]["error_code"], "EXACT_QA_LOOKUP_FAILED")
        first = events[0]["data"]
        run = await self.repository.get_run(first["conversation_id"], first["run_id"], user_id="101")
        self.assertEqual(run.status, "failed")
        self.assertIsNone(self.service._agent)

    async def test_miss_uses_model_and_persists_shared_reasoning(self):
        self.matcher.match.return_value = None

        class Agent:
            model_name = "fake"
            async def run_stream(self, question, history, run_id="-"):
                yield AgentEvent(type="reasoning_delta", content="思考\n", round_number=1)
                yield AgentEvent(type="reasoning_delta", content="续段", round_number=1)
                yield AgentEvent(type="answer_delta", content="生成正文", round_number=1)

        self.service._agent = Agent()
        events = await self.events()
        done = next(e["data"] for e in events if e["event"] == "done")
        self.assertEqual(done["answer_origin"], "model")
        self.assertEqual(done["reasoning"]["blocks"][0]["content"], "思考\n续段")
        self.assertEqual(done["reasoning"]["blocks"][0]["status"], "completed")
        saved = await self.repository.get(done["conversation_id"], user_id="101")
        self.assertEqual(saved.messages[-1].reasoning.model_dump(), done["reasoning"])
        steps = [e["data"] for e in events if e["event"] == "process" and e["data"].get("display_only")]
        self.assertEqual([step["summary"] for step in steps], ["思考\n", "思考\n续段", "思考\n续段"])
        self.assertEqual(steps[-1]["status"], "success")

    async def test_fixed_answer_can_be_cancelled_with_partial_reasoning(self):
        original = self.repository.save_reasoning
        reached = asyncio.Event()
        release = asyncio.Event()

        async def blocking_save(*args, **kwargs):
            await original(*args, **kwargs)
            if not reached.is_set():
                reached.set()
                await release.wait()

        with patch.object(self.repository, "save_reasoning", side_effect=blocking_save):
            stream = self.service.stream_chat(AgentRequest(question=snapshot().question), user_id="101")
            first = await stream.__anext__()
            await reached.wait()
            await self.service.cancel_run(first["data"]["conversation_id"], first["data"]["run_id"], user_id="101")
            remaining = [e async for e in stream]
        self.assertEqual(remaining[-1]["event"], "cancelled")
        self.assertEqual(remaining[-1]["data"]["reasoning"]["blocks"][0]["status"], "cancelled")
        self.assertFalse(any(e["event"] == "done" for e in remaining))
        steps = [e["data"] for e in remaining if e["event"] == "process"]
        self.assertEqual(steps[-1]["status"], "skipped")

    async def test_pacing_uses_configured_character_rate_and_preserves_text(self):
        self.assertEqual(ExactQaSettings().chars_per_second, 40)
        for invalid in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                ExactQaSettings(chars_per_second=invalid)
        for rate in (40, 20):
            settings = SimpleNamespace(exact_qa=ExactQaSettings(chars_per_second=rate))
            text = " 原文🌊\r\n末尾  "
            with patch("services.chat_service.get_settings", return_value=settings), \
                 patch("services.chat_service.asyncio.sleep", new_callable=AsyncMock) as sleep:
                chunks = [chunk async for chunk in self.service._paced_exact_qa_chunks(text)]
            self.assertEqual("".join(chunks), text)
            self.assertEqual([call.args[0] for call in sleep.await_args_list], [len(chunk) / rate for chunk in chunks])
            self.assertAlmostEqual(sum(call.args[0] for call in sleep.await_args_list), len(text) / rate)


class ExactQaClientTest(unittest.IsolatedAsyncioTestCase):
    async def call(self, handler):
        settings = SimpleNamespace(exact_qa=SimpleNamespace(enabled=True, timeout=5),
            dictionaries=SimpleNamespace(base_url="http://knowledge", connect_timeout=1, verify_ssl=True),
            internal_request=InternalRequestSettings(token="unit-test-secret-token"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("services.exact_qa_service.get_settings", return_value=settings), \
             patch("services.exact_qa_service.httpx.AsyncClient", return_value=client):
            return await ExactQaService().match(snapshot().question)

    async def test_request_preserves_question_and_internal_token(self):
        def handler(request):
            self.assertEqual(json.loads(request.content)["question"], snapshot().question)
            self.assertEqual(request.headers["X-DH-Internal-Token"], "unit-test-secret-token")
            return httpx.Response(200, json={"code": 200, "data": snapshot().model_dump(by_alias=True)})
        result = await self.call(handler)
        self.assertEqual(result.answer, snapshot().answer)

    async def test_null_is_miss_but_server_error_is_failure(self):
        self.assertIsNone(await self.call(lambda request: httpx.Response(200, json={"code": 200, "data": None})))
        with self.assertRaises(httpx.HTTPStatusError):
            await self.call(lambda request: httpx.Response(500))

    async def test_different_question_is_failure(self):
        data = snapshot().model_dump(by_alias=True)
        data["question"] = snapshot().question.strip()
        with self.assertRaises(RuntimeError):
            await self.call(lambda request: httpx.Response(200, json={"code": 200, "data": data}))


class UpstreamReasoningTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_adapter_preserves_reasoning_extension_without_diagnostics(self):
        client = DiagnosticChatOpenAI(model="fake", api_key="test")
        chunk = client._convert_chunk_to_generation_chunk({"choices": [{"index": 0,
            "delta": {"role": "assistant", "content": "", "reasoning_content": " 思考\n "},
            "finish_reason": None}]}, AIMessageChunk, {})
        self.assertEqual(chunk.message.additional_kwargs["reasoning_content"], " 思考\n ")

    async def test_llm_stream_separates_reasoning_from_body(self):
        class Client:
            async def astream(self, *args, **kwargs):
                yield AIMessageChunk(content="", additional_kwargs={"reasoning_content": "思考\n"})
                yield AIMessageChunk(content="正文")
        llm = object.__new__(HelloAgentsLLM)
        llm.client = Client()
        llm.diagnostics_enabled = False
        reasoning = []
        chunks = [chunk async for chunk in llm.stream([], reasoning_callback=reasoning.append)]
        self.assertEqual(reasoning, ["思考\n"])
        self.assertEqual("".join(content for content, calls in chunks), "正文")

    async def test_agent_loop_emits_reasoning_before_answer(self):
        class Llm:
            model = "fake"
            async def stream(self, *args, reasoning_callback=None, **kwargs):
                reasoning_callback("原始思考")
                yield "", []
                yield "正文", []
        with patch("services.context_service.expand_query_synonyms", side_effect=lambda x: x):
            agent = AgentLoop(llm=Llm(), tools=ToolExecutor())
            events = [e async for e in agent.run_stream("问题", [])]
        self.assertEqual([e.type for e in events], ["reasoning_delta", "answer_delta"])
        self.assertEqual(events[0].content, "原始思考")

    def test_duplicate_reasoning_ids_rejected(self):
        value = snapshot().reasoning.model_dump()
        value["blocks"].append(value["blocks"][0])
        with self.assertRaises(ValueError):
            ReasoningSnapshot.model_validate(value)
