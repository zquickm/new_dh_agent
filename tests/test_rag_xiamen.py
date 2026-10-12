import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.rag_search import RagSearchTool
from config.settings import get_settings
from schemas.chat import AgentRequest
from services.chat_service import ChatService
from services.synonym_service import build_synonym_matcher
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm
from tests.test_rag_reranker import FakeResponse, FakeReranker, FakeSession, make_backend
from tool_backends.rag_models import RagToolError


class RagXiamenTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        matcher = patch("services.synonym_service._active_synonym_matcher", build_synonym_matcher([
            {"standardTerm": "厦门", "terms": [{"term": "鹭岛"}]},
            {"standardTerm": "溶解氧", "terms": [{"term": "DO"}]},
        ]))
        matcher.start()
        self.addCleanup(matcher.stop)

    async def test_query_routes_and_process_names_without_changing_agent_loop(self):
        settings = get_settings()
        default_ids = [b.knowledge_id for b in settings.business_scenarios["general"].rag.knowledge_bases]
        xiamen_ids = [b.knowledge_id for b in settings.business_scenarios["xiamen"].rag.knowledge_bases]
        session = FakeSession(*(
            [FakeResponse({"code": 200, "data": {"records": []}})] * 19
            + [RuntimeError("厦门库不可用")]
        ))
        backend = make_backend(session, FakeReranker())
        backend.knowledge_ids = tuple(xiamen_ids)
        tool = RagSearchTool(backend)
        tools = ToolExecutor()
        tools.register("rag_search", "检索", tool.execute)
        tools.register_closer(tool.close)
        queries = ("海洋图书", "厦门DO", "疑点疑区制度", "图斑依据", "鹭岛业务", "厦门失败")
        calls = [{"id": str(i), "name": "rag_search", "args": {"query": query}}
                 for i, query in enumerate(queries)]
        service = ChatService()
        service._agent = AgentLoop(FakeLlm([([], calls), (["完成"], [])]), tools)
        self.addAsyncCleanup(service.close)
        with patch("services.chat_service.conversation_repository", MemoryConversationRepository()), patch(
            "services.chat_service.generate_recommendations", return_value=[],
        ):
            events = [event async for event in service.stream_chat(
                AgentRequest(question="请查询业务资料"), user_id="101",
            )]
        self.assertEqual([call["json"]["knowledge_ids"] for call in session.calls],
                         [[kid] for kid in default_ids + xiamen_ids * 4 + default_ids[:1]])
        self.assertEqual(session.calls[15]["json"]["query"], ["鹭岛（厦门）业务"])
        self.assertEqual([call["json"]["top_n"] for call in session.calls],
                         [b.recall_top_n for b in settings.business_scenarios["general"].rag.knowledge_bases]
                         + [b.recall_top_n for b in settings.business_scenarios["xiamen"].rag.knowledge_bases] * 4
                         + [settings.business_scenarios["xiamen"].rag.knowledge_bases[0].recall_top_n])
        self.assertTrue(all(call["json"]["score"] == 0.6 for call in session.calls))
        for i in range(len(queries)):
            name = "东溟知识库检索" if i == 0 else "厦门局业务知识库"
            process = [event["data"] for event in events
                       if event["event"] == "process" and event["data"]["step_id"] == str(i)]
            self.assertEqual([item["title"] for item in process], [f"算法工具箱：{name}"] * 2)
            self.assertEqual(process[0]["summary"], f"{name}正在执行")
            self.assertEqual(process[1]["status"], "failed" if i == 5 else "success")
            if i != 5:
                libraries = settings.business_scenarios["general" if i == 0 else "xiamen"].rag.knowledge_bases
                counts = "、".join(f"{library.name} 0 份" for library in libraries)
                self.assertEqual(process[1]["summary"],
                    f"已检索 {len(libraries)} 个知识库，最终筛选后文件数量（各库内去重）：{counts}。")
                self.assertNotIn("查询：“", process[1]["summary"])
                self.assertNotIn("original_query", process[1]["details"])
                self.assertEqual(process[1]["details"]["library_count"], len(libraries))
            else:
                self.assertNotIn("最终筛选后", process[1]["summary"])
                self.assertEqual(process[1]["details"], {})

    async def test_missing_xiamen_config_and_unauthorized_library_send_no_requests(self):
        settings = get_settings().model_copy(deep=True)
        settings.business_scenarios.pop("xiamen")
        backend = SimpleNamespace(search_all=AsyncMock())
        with patch("agents.tools.rag_search.get_settings", return_value=settings):
            with self.assertRaisesRegex(ValueError, "当前场景"):
                await RagSearchTool(backend).execute("鹭岛业务")
        backend.search_all.assert_not_awaited()
        session = FakeSession()
        backend = make_backend(session, FakeReranker())
        for libraries in ([], [("other", 20, 4)], [("knowledge-1", 20, 4)] * 2):
            with self.assertRaises(RagToolError):
                await backend.search_all("厦门", libraries)
        self.assertEqual(session.calls, [])

    async def test_scenario_thresholds_are_local_to_concurrent_requests(self):
        settings = get_settings().model_copy(deep=True)
        settings.business_scenarios["xiamen"].rag.score = 0.8  # Distinct values prove the selected scenario supplies the request score.
        settings.business_scenarios["xiamen"].rag.result_score_threshold = 0.2  # Keep thresholds distinct regardless of deployment tuning.
        backend = make_backend(FakeSession(), FakeReranker())
        backend.knowledge_ids = tuple(b.knowledge_id for b in settings.business_scenarios["xiamen"].rag.knowledge_bases)
        payloads = []

        async def recall(url, payload, headers, **kwargs):
            payloads.append(payload)
            await asyncio.sleep(0)
            return {"code": 200, "data": {"records": [
                {"chunk_text": str(score), "fileName": "资料.pdf", "score": score,
                 "knowledge_id": payload["knowledge_ids"][0]}
                for score in (0.19, 0.2, 0.25, 0.3)
            ]}}

        with patch("agents.tools.rag_search.get_settings", return_value=settings), patch.object(backend, "_post_json", side_effect=recall):
            tool = RagSearchTool(backend)
            general, xiamen = await asyncio.gather(tool.execute("海洋图书"), tool.execute("鹭岛业务"))
        self.assertEqual(general["count"], 3)
        self.assertEqual({chunk["score"] for chunk in general["chunks"]}, {0.3})
        self.assertEqual(xiamen["count"], 12)
        self.assertEqual({chunk["score"] for chunk in xiamen["chunks"]}, {0.2, 0.25, 0.3})
        self.assertEqual({p["score"] for p in payloads if p["query"] == ["海洋图书"]}, {0.6})
        self.assertEqual({p["score"] for p in payloads if p["query"] == ["鹭岛（厦门）业务"]}, {0.8})
        self.assertEqual((backend.score, backend.result_score_threshold), (0.6, 0.6))

    async def test_scenario_per_library_caps_and_default_configuration(self):
        settings = get_settings()
        self.assertEqual((settings.business_scenarios["general"].rag.score, settings.business_scenarios["general"].rag.result_score_threshold), (0.6, 0.3))
        self.assertEqual((settings.business_scenarios["xiamen"].rag.score, settings.business_scenarios["xiamen"].rag.result_score_threshold), (0.6, 0.3))
        backend = make_backend(FakeSession(FakeResponse({"code": 200, "data": {"records": [
            {"chunk_text": str(i), "fileName": "资料.pdf", "score": 0.9} for i in range(50)
        ]}})), FakeReranker())
        backend.knowledge_ids = tuple(b.knowledge_id for b in settings.business_scenarios["xiamen"].rag.knowledge_bases)
        tool = RagSearchTool(backend)
        self.assertEqual((await tool.execute("海洋图书"))["count"],
                         sum(min(50, b.top_n) for b in settings.business_scenarios["general"].rag.knowledge_bases))
        self.assertEqual((await tool.execute("厦门业务"))["count"],
                         sum(min(50, b.top_n) for b in settings.business_scenarios["xiamen"].rag.knowledge_bases))
