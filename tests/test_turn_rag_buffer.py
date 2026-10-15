import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from pydantic import SecretStr

from agents.agent_loop import AgentLoop
from agents.citations import CitationRegistry
from agents.rag_buffer import RagChunkBuffer
from agents.tools.executor import ToolExecutor
from config.settings import get_settings
from services.token_budget_service import DeepSeekV4TokenCounter, TokenBudgetService
from services.synonym_service import build_synonym_matcher
from tests.test_agent_loop import FakeLlm


def chunk(number, score=0.5, **values):
    return {"text": f"证据{number}：" + "海洋监测数据。" * 100, "source": "报告.pdf",
            "knowledge_id": "kb", "chunk_id": str(number), "score": score, **values}


class TurnRagBufferTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counter = DeepSeekV4TokenCounter(get_settings().llm.resolved_tokenizer_path())
        self.registry = CitationRegistry()
        self.rerank = AsyncMock(side_effect=lambda query, chunks: [
            replace(c, score={"<<SRC_1>>": 0.8, "<<SRC_2>>": 0.1, "<<SRC_3>>": 0.9}[c.chunk_id])
            for c in chunks
        ])
        self.buffer = RagChunkBuffer("近五年断面通报", 60000, self.counter, self.rerank)
        self.messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "问题"}]

    def add(self, chunks, call="call"):
        _, observation = self.registry.register_rag_observation(
            json.dumps({"query": "2022年断面通报", "requested_tags": ["2022年"], "chunks": chunks}),
            tool_call_id=call, fallback_query="",
        )
        return json.loads(self.buffer.add(observation))

    async def fit(self, limit=120832, messages=None, groups=()):
        return await self.buffer.fit(
            TokenBudgetService(self.counter, limit), messages or self.messages,
            history_group_sizes=groups, original_history_count=sum(groups), tools=None,
        )

    def material(self):
        message = self.buffer.message()
        return json.loads(message["content"].split("\n", 1)[1]) if message else {"chunks": []}

    async def test_deduplicates_across_calls_but_keeps_same_page_paragraphs_and_versions(self):
        first = chunk(1, page_index=0, tags=["2022年"])
        self.add([first])
        summary = self.add([first, chunk(2, page_index=0), {**first, "text": "修订正文"}], "next")
        self.assertEqual(summary["duplicate_count"], 1)
        self.assertEqual(summary["source_keys"], ["<<SRC_1>>", "<<SRC_2>>", "<<SRC_3>>"])
        self.assertNotIn("修订正文", json.dumps(summary, ensure_ascii=False))
        fitted = await self.fit()
        self.assertEqual(len(self.material()["chunks"]), 3)
        self.assertEqual(sum(m.get("name") == "rag_buffer" for m in fitted.messages), 1)
        self.rerank.assert_not_awaited()

    async def test_no_id_fallback_uses_document_location_and_text(self):
        first = chunk(1, chunk_id=None, document_id="doc", page_index=0, line=1)
        self.add([first])
        summary = self.add([first, {**first, "line": 2}, {**first, "document_id": "other"}])
        self.assertEqual(summary["duplicate_count"], 1)
        self.assertEqual(len(self.material()["chunks"]), 3)

    async def test_exact_budget_keeps_all_without_reranking(self):
        self.add([chunk(1), chunk(2)])
        self.buffer.token_budget = self.buffer.tokens
        await self.fit()
        self.assertEqual(len(self.material()["chunks"]), 2)
        self.assertEqual(self.buffer.tokens, self.buffer.token_budget)
        self.rerank.assert_not_awaited()

    async def test_overflow_removes_lowest_rerank_score_not_lowest_recall(self):
        self.add([chunk(1, 0.1), chunk(2, 0.99), chunk(3, 0.2)])
        self.buffer.token_budget = self.buffer.tokens - 1
        await self.fit()
        self.assertEqual([c["source_key"] for c in self.material()["chunks"]], ["<<SRC_1>>", "<<SRC_3>>"])
        self.assertEqual(self.material()["omitted_count"], 1)
        self.assertLessEqual(self.buffer.tokens, self.buffer.token_budget)
        query, candidates = self.rerank.await_args.args
        self.assertEqual(query, "近五年断面通报")
        self.assertEqual(len(candidates), 3)

    async def test_ties_remove_newer_first_and_can_remove_multiple(self):
        self.rerank.side_effect = lambda query, chunks: [replace(c, score=0.5) for c in chunks]
        self.add([chunk(1)])
        one_chunk_tokens = self.buffer.tokens
        self.add([chunk(2), chunk(3)])
        self.buffer.token_budget = one_chunk_tokens + 5
        await self.fit()
        self.assertEqual([c["source_key"] for c in self.material()["chunks"]], ["<<SRC_1>>"])
        self.assertEqual(self.material()["omitted_count"], 2)

    async def test_tokens_include_metadata_and_oversized_chunks_are_removed_whole(self):
        self.add([chunk(1, text="短正文", source="很长的文档名称" * 300)])
        self.assertGreater(self.buffer.tokens, self.counter.count_text("短正文"))
        self.buffer.token_budget = 100
        fitted = await self.fit()
        self.assertEqual(self.buffer.tokens, 0)
        self.assertEqual(fitted.messages, self.messages)
        self.assertNotIn("短正文", json.dumps(fitted.messages, ensure_ascii=False))

    async def test_rerank_failure_or_invalid_scores_fall_back_for_all_candidates(self):
        for invalid in (RuntimeError("服务不可用"), "nan", "incomplete", "duplicate"):
            with self.subTest(invalid=invalid):
                self.buffer.clear()
                self.add([chunk(1, 0.9), chunk(2, 0.1), chunk(3, 0.8)])
                self.buffer.token_budget = self.buffer.tokens - 1
                if isinstance(invalid, Exception):
                    self.rerank.side_effect = invalid
                else:
                    def invalid_ranking(query, chunks):
                        if invalid == "nan":
                            return [replace(c, score=float("nan")) for c in chunks]
                        return chunks[:1] if invalid == "incomplete" else [chunks[0]] * len(chunks)
                    self.rerank.side_effect = invalid_ranking
                with self.assertLogs("agents.rag_buffer", level="WARNING"):
                    await self.fit()
                self.assertEqual(self.buffer.ranking_source, "recall_fallback")
                self.assertEqual([c["source_key"] for c in self.material()["chunks"]], ["<<SRC_1>>", "<<SRC_3>>"])

    async def test_global_budget_drops_history_before_evidence(self):
        self.add([chunk(1), chunk(2)])
        history = [{"role": "user", "content": "旧问题" * 300}, {"role": "assistant", "content": "旧回答" * 300}]
        messages = [self.messages[0], *history, self.messages[1]]
        limit = self.counter.count_request([*messages, self.buffer.message()]) - 1
        fitted = await self.fit(limit, messages, (2,))
        self.assertEqual(fitted.history_messages_dropped, 2)
        self.assertEqual(len(self.material()["chunks"]), 2)
        self.rerank.assert_not_awaited()

    async def test_global_budget_removes_slices_before_cutting_other_tools(self):
        self.add([chunk(1), chunk(2), chunk(3)])
        business = {"role": "tool", "name": "database_query", "content": "业务数据" * 150}
        messages = [*self.messages, business]
        limit = self.counter.count_request([*messages, self.buffer.message()]) - 1
        fitted = await self.fit(limit, messages)
        self.assertEqual(fitted.messages[-2], business)
        self.assertEqual(fitted.tool_results_truncated, 0)
        self.assertEqual(len(self.material()["chunks"]), 2)
        self.assertLessEqual(fitted.estimated_input_tokens, limit)

    async def test_all_slices_removed_before_existing_tool_truncation_fallback(self):
        self.add([chunk(1), chunk(2)])
        messages = [*self.messages, {"role": "tool", "content": "业务数据" * 5000}]
        fitted = await self.fit(200, messages)
        self.assertFalse(self.buffer.has_chunks)
        self.assertEqual(fitted.tool_results_truncated, 1)
        self.assertLessEqual(fitted.estimated_input_tokens, 200)

    async def test_evicted_chunk_can_return_with_its_original_source_key(self):
        self.add([chunk(1), chunk(2)])
        self.buffer.token_budget = self.buffer.tokens - 1
        await self.fit()
        summary = self.add([chunk(2)], "again")
        self.assertEqual(summary["source_keys"], ["<<SRC_2>>"])
        self.buffer.token_budget = 60000
        await self.fit()
        self.assertEqual(self.material()["omitted_count"], 0)


class TurnRagBufferLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        synonym_patch = patch("services.synonym_service._active_synonym_matcher", build_synonym_matcher([
            {"standardTerm": "溶解氧", "terms": [{"term": "DO"}]},
        ]))
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)

    async def test_agent_uses_configured_reranker_once_for_batch_and_keeps_only_budgeted_body(self):
        settings = get_settings().model_copy(deep=True)
        settings.tools.rag.api_token = SecretStr("test-token")
        settings.tools.rag.rerank.enabled = True
        counter = DeepSeekV4TokenCounter(settings.llm.resolved_tokenizer_path())
        registry = CitationRegistry()
        _, observation = registry.register_rag_observation(
            json.dumps({"chunks": [chunk(1)]}), tool_call_id="size", fallback_query="",
        )
        sizing = RagChunkBuffer("问题", 60000, counter, AsyncMock())
        sizing.add(observation)
        settings.tools.rag.buffer_token_budget = sizing.tokens + 5

        async def retrieve(query):
            return {"chunks": [chunk(1), chunk(2), chunk(3)]}

        reranker = AsyncMock()
        reranker.rerank.side_effect = lambda query, chunks, top_n: [replace(c, score=i) for i, c in enumerate(chunks)]
        llm = FakeLlm([([], [{"id": "rag", "name": "rag_search", "args": {"query": "2022年报告"}},
                             {"id": "repeat", "name": "rag_search", "args": {"query": "2023年报告"}}]),
                       (["结论<<SRC_3>>"], [])])
        tools = ToolExecutor()
        tools.register("rag_search", "检索", retrieve)
        with patch("agents.agent_loop.get_settings", return_value=settings), patch(
            "agents.agent_loop.VllmDocumentReranker", return_value=reranker,
        ) as factory:
            agent = AgentLoop(llm, tools)
            saved = []
            try:
                events = [event async for event in agent.run_stream(
                    "近五年断面通报", [], model_messages_callback=saved.append,
                )]
                material = next(m for m in llm.calls[-1][0] if m.get("name") == "rag_buffer")
                self.assertIn(chunk(3)["text"], material["content"])
                self.assertNotIn(chunk(1)["text"], material["content"])
                self.assertNotIn(chunk(2)["text"], material["content"])
                self.assertLessEqual(counter.count_request([material]), settings.tools.rag.buffer_token_budget)
                self.assertNotIn(chunk(3)["text"], json.dumps(saved, ensure_ascii=False))
                self.assertEqual("".join(e.content for e in events if e.type == "answer_delta"), "结论[[cite:1]]")
                factory.assert_called_once()
                query, candidates, top_n = reranker.rerank.await_args.args
                self.assertEqual((query, len(candidates), top_n), ("近五年断面通报", 3, 3))
                reranker.rerank.assert_awaited_once()
            finally:
                await agent.close()
        reranker.close.assert_awaited_once()

    async def test_success_error_and_stream_close_clear_the_buffer(self):
        for outcome in ("success", "error", "close"):
            with self.subTest(outcome=outcome):
                async def retrieve(query):
                    return {"chunks": [chunk(1)]}
                llm = FakeLlm([([], [{"id": "rag", "name": "rag_search", "args": {"query": "资料"}}]),
                               (["完成<<SRC_1>>"], [])] if outcome != "error" else
                              [([], [{"id": "rag", "name": "rag_search", "args": {"query": "资料"}}])])
                tools = ToolExecutor()
                tools.register("rag_search", "检索", retrieve)
                agent = AgentLoop(llm, tools)
                self.addAsyncCleanup(agent.close)
                buffers = []

                def create(*args):
                    buffer = RagChunkBuffer(*args)
                    buffers.append(buffer)
                    return buffer

                with patch("agents.agent_loop.RagChunkBuffer", side_effect=create):
                    stream = agent.run_stream("资料", [])
                    if outcome == "close":
                        for _ in range(3):
                            await stream.__anext__()
                        self.assertTrue(buffers[0].has_chunks)
                        await stream.aclose()
                    elif outcome == "error":
                        with self.assertRaises(RuntimeError):
                            _ = [event async for event in stream]
                    else:
                        _ = [event async for event in stream]
                self.assertFalse(buffers[0].has_chunks)
                self.assertEqual(buffers[0].tokens, 0)

    async def test_concurrent_requests_have_independent_material_and_citations(self):
        model_inputs = []

        class Llm:
            model = "fake"

            async def stream(self, messages, tools=None, temperature=0):
                question = next(m["content"] for m in messages if m["role"] == "user")
                material = next((m for m in messages if m.get("name") == "rag_buffer"), None)
                await asyncio.sleep(0)
                if material is None:
                    yield "", [{"id": "rag", "name": "rag_search", "args": {"query": question}}]
                else:
                    model_inputs.append((question, material["content"]))
                    yield "完成<<SRC_1>>", []

        async def retrieve(query):
            await asyncio.sleep(0)
            return {"chunks": [chunk(1, text=query + "独立证据")]}

        tools = ToolExecutor()
        tools.register("rag_search", "检索", retrieve)
        agent = AgentLoop(Llm(), tools)
        self.addAsyncCleanup(agent.close)

        async def run(question):
            return [event async for event in agent.run_stream(question, [])]

        await asyncio.gather(run("问题甲"), run("问题乙"))
        self.assertEqual(len(model_inputs), 2)
        for question, material in model_inputs:
            self.assertIn(question + "独立证据", material)
            self.assertNotIn(("问题乙" if question == "问题甲" else "问题甲") + "独立证据", material)

    async def test_task_cancellation_clears_material(self):
        started = asyncio.Event()

        class Llm:
            model = "fake"

            async def stream(self, messages, tools=None, temperature=0):
                if not any(m.get("name") == "rag_buffer" for m in messages):
                    yield "", [{"id": "rag", "name": "rag_search", "args": {"query": "资料"}}]
                else:
                    started.set()
                    await asyncio.Event().wait()

        async def retrieve(query):
            return {"chunks": [chunk(1)]}

        tools = ToolExecutor()
        tools.register("rag_search", "检索", retrieve)
        agent = AgentLoop(Llm(), tools)
        self.addAsyncCleanup(agent.close)
        buffers = []

        def create(*args):
            buffer = RagChunkBuffer(*args)
            buffers.append(buffer)
            return buffer

        async def consume():
            return [event async for event in agent.run_stream("资料", [])]

        with patch("agents.agent_loop.RagChunkBuffer", side_effect=create):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(buffers[0].has_chunks)
