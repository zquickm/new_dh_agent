from __future__ import annotations

import unittest
import asyncio
from typing import Any, Optional

from aiohttp import web
from tool_backends.rag_backend import RagSearchBackend
from tool_backends.rag_models import RagChunk, RagToolError
from tool_backends.rerank_backend import (
    INSTRUCTION,
    PREFIX,
    SUFFIX,
    VllmDocumentReranker,
    VllmRerankerSettings,
)


class FakeResponse:
    def __init__(self, body: Any, status: int = 200) -> None:
        self.body = body
        self.status = status

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self, **_kwargs: Any) -> Any:
        return self.body

    async def text(self) -> str:
        return str(self.body)


class FakeSession:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        response = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self) -> None:
        self.closed = True


class FakeReranker:
    def __init__(self, error: Optional[RagToolError] = None) -> None:
        self.error = error
        self.calls: list[tuple[str, list[RagChunk], int]] = []
        self.closed = False

    async def rerank(
        self,
        query: str,
        chunks: list[RagChunk],
        top_n: int,
    ) -> list[RagChunk]:
        self.calls.append((query, chunks, top_n))
        if self.error is not None:
            raise self.error
        return list(reversed(chunks))[:top_n]

    async def close(self) -> None:
        self.closed = True


def make_chunks() -> list[RagChunk]:
    return [
        RagChunk(
            text="北京是中华人民共和国的首都。",
            source="北京.pdf",
            score=0.2,
            chunk_id="beijing",
            knowledge_id="knowledge-1",
            page_index=2,
        ),
        RagChunk(
            text="上海是中国重要的经济中心。",
            source="上海.pdf",
            score=0.8,
            chunk_id="shanghai",
            knowledge_id="knowledge-1",
            page_index=4,
        ),
    ]


def make_reranker(session: FakeSession, **changes: Any) -> VllmDocumentReranker:
    values = {
        "url": "http://rerank.example:18000/v1/rerank",
        "model": "Qwen3-Reranker-4B",
        "api_token": "test-token",
        "timeout": 15,
        "connect_timeout": 5,
        "max_retries": 0,
        "retry_backoff": 0,
        "verify_ssl": True,
    }
    values.update(changes)
    return VllmDocumentReranker(VllmRerankerSettings(**values), session)


def make_backend(session: FakeSession, reranker: FakeReranker) -> RagSearchBackend:
    backend = RagSearchBackend(
        recall_url="http://rag.example/recall",
        api_token="test-token",
        tenant_id="tenant-1",
        knowledge_ids=("knowledge-1",),
        dept_ids=("dept-1",),
        score=0.6,
        candidate_top_n=2,
        faq_score=0.8,
        search_method=3,
        search_scope="knowledge",
        timeout=30,
        connect_timeout=5,
        connection_limit=10,
        verify_ssl=True,
        max_retries=0,
        retry_backoff=0,
        rerank_enabled=True,
        rerank_url="http://rerank.example/v1/rerank",
        rerank_model="Qwen3-Reranker-4B",
        rerank_timeout=15,
        rerank_connect_timeout=5,
        rerank_max_retries=0,
        rerank_retry_backoff=0,
        rerank_verify_ssl=True,
        reranker=reranker,
    )
    backend._session = session
    return backend


class VllmDocumentRerankerTest(unittest.IsolatedAsyncioTestCase):
    async def test_sends_model_template_and_restores_metadata_by_index(self) -> None:
        session = FakeSession(
            FakeResponse(
                {
                    "results": [
                        {"index": 1, "relevance_score": 0.1},
                        {"index": 0, "relevance_score": 0.99},
                    ]
                }
            )
        )
        reranker = make_reranker(session)

        results = await reranker.rerank("中国的首都是哪里？", make_chunks(), 20)

        self.assertEqual([item.chunk_id for item in results], ["beijing", "shanghai"])
        self.assertEqual(results[0].source, "北京.pdf")
        self.assertEqual(results[0].page_index, 2)
        self.assertEqual(results[0].score, 0.99)
        call = session.calls[0]
        self.assertEqual(call["url"], "http://rerank.example:18000/v1/rerank")
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(call["headers"]["Accept"], "application/json")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertTrue(call["ssl"])
        self.assertEqual(call["json"]["top_n"], 2)
        self.assertEqual(
            call["json"]["query"],
            f"{PREFIX}<Instruct>: {INSTRUCTION}\n<Query>: 中国的首都是哪里？\n",
        )
        self.assertEqual(
            call["json"]["documents"][0],
            f"<Document>: 北京是中华人民共和国的首都。{SUFFIX}",
        )

    async def test_empty_candidates_do_not_call_service(self) -> None:
        session = FakeSession()
        reranker = make_reranker(session)

        self.assertEqual(await reranker.rerank("query", [], 5), [])
        self.assertEqual(session.calls, [])

    async def test_invalid_or_incomplete_results_are_rejected(self) -> None:
        invalid = make_reranker(
            FakeSession(FakeResponse({"results": [{"index": 9, "relevance_score": 1}]}))
        )
        with self.assertRaisesRegex(RagToolError, "invalid index"):
            await invalid.rerank("query", make_chunks(), 1)

        incomplete = make_reranker(
            FakeSession(FakeResponse({"results": [{"index": 0, "relevance_score": 1}]}))
        )
        with self.assertRaisesRegex(RagToolError, "expected 2"):
            await incomplete.rerank("query", make_chunks(), 2)

    async def test_retries_retryable_http_errors(self) -> None:
        session = FakeSession(
            FakeResponse("busy", status=429),
            FakeResponse({"results": [{"index": 0, "relevance_score": 0.9}]}),
        )
        reranker = make_reranker(session, max_retries=1)

        results = await reranker.rerank("query", make_chunks(), 1)

        self.assertEqual(results[0].chunk_id, "beijing")
        self.assertEqual(len(session.calls), 2)


class RagRerankFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_three_http_requests_share_token_and_select_distinct_libraries(self):
        calls = []
        all_started = asyncio.Event()

        async def recall(request):
            payload = await request.json()
            calls.append((request.method, request.headers["Authorization"], payload))
            if len(calls) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), 2)
            knowledge_id = payload["knowledge_ids"][0]
            count = {"policy": 6, "business": 2, "literature": 0}[knowledge_id]
            return web.json_response({"code": 200, "data": {"records": [
                {"chunk_text": f"{knowledge_id}-{i}", "knowledge_id": knowledge_id,
                 "doc_id": f"doc-{i}", "chunk_id": f"chunk-{i}", "doc_title": "报告", "score": [0.61, 0.95, 0.7, 0.95, 0.8, 0.9][i]}
                for i in range(count)
            ]}})

        app = web.Application()
        app.router.add_post("/recall", recall)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        backend = make_backend(FakeSession(), FakeReranker())
        backend._session = None
        backend.recall_url = f"http://127.0.0.1:{runner.addresses[0][1]}/recall"
        backend.knowledge_ids = ("policy", "business", "literature")
        backend.candidate_top_n = 4
        self.addAsyncCleanup(backend.close)

        result = await backend.search_all("断面调查",
            [("policy", 20, 3), ("business", 10, 4), ("literature", 5, 2)], {"tags": ["断面"]})

        self.assertEqual([chunk.knowledge_id for chunk in result], ["policy"] * 3 + ["business"] * 2)
        self.assertEqual([chunk.text for chunk in result],
                         ["policy-1", "policy-3", "policy-5", "business-1", "business-0"])
        self.assertEqual(backend.reranker.calls, [])
        self.assertEqual(len(calls), 3)  # Empty/sparse results do not trigger more requests.
        self.assertEqual({tuple(payload["knowledge_ids"]) for _, _, payload in calls},
                         {("policy",), ("business",), ("literature",)})
        for method, authorization, payload in calls:
            self.assertEqual(method, "POST")
            self.assertEqual(authorization, "Bearer test-token")
            self.assertEqual(payload["query"], ["断面调查"])
            self.assertEqual(payload["tags"], ["断面"])
            self.assertEqual(payload["score"], 0.6)
            self.assertEqual(payload["top_n"], {"policy": 20, "business": 10, "literature": 5}[payload["knowledge_ids"][0]])

    async def test_invalid_library_configuration_does_not_send_requests(self):
        for ids in (("one",), ("one", "two", ""), ("one", "two", "one")):
            with self.subTest(ids=ids):
                session = FakeSession()
                backend = make_backend(session, FakeReranker())
                backend.knowledge_ids = ids
                with self.assertRaises(RagToolError):
                    await backend.search_all("断面", [(kid, 20, 4) for kid in ids])
                self.assertEqual(session.calls, [])

    async def test_recall_candidates_are_reranked_before_final_limit(self) -> None:
        records = [
            {"chunk_text": chunk.text, "fileName": chunk.source, "chunk_id": chunk.chunk_id}
            for chunk in make_chunks()
        ]
        session = FakeSession(FakeResponse({"code": 200, "data": {"records": records}}))
        reranker = FakeReranker()
        backend = make_backend(session, reranker)

        results = await backend.search("首都", 1)

        self.assertEqual(session.calls[0]["json"]["top_n"], 2)
        self.assertEqual(session.calls[0]["json"]["rerank_enable"], 0)
        self.assertEqual(len(reranker.calls[0][1]), 2)
        self.assertEqual(results[0].chunk_id, "shanghai")

    async def test_rerank_failure_falls_back_and_close_closes_both_clients(self) -> None:
        records = [
            {"chunk_text": chunk.text, "fileName": chunk.source, "chunk_id": chunk.chunk_id}
            for chunk in make_chunks()
        ]
        session = FakeSession(FakeResponse({"code": 200, "data": {"records": records}}))
        reranker = FakeReranker(RagToolError("service unavailable"))
        backend = make_backend(session, reranker)

        with self.assertLogs("tool_backends.rag_backend", level="WARNING"):
            results = await backend.search("首都", 1)
        await backend.close()

        self.assertEqual(results[0].chunk_id, "beijing")
        self.assertTrue(session.closed)
        self.assertTrue(reranker.closed)


if __name__ == "__main__":
    unittest.main()
