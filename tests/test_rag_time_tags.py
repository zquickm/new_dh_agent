import json
import unittest
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from agents.tools.rag_search import RagSearchTool
from config.settings import RagKnowledgeBaseSettings, get_settings
from services.context_service import build_system_prompt
from services.synonym_service import build_synonym_matcher
from tests.test_agent_loop import FakeLlm
from tests.test_rag_reranker import FakeResponse, FakeReranker, FakeSession, make_backend
from tool_backends.rag_backend import RagSearchBackend


def recall_response(records):
    return FakeResponse({"code": 200, "data": {"records": records}})


class RagTimeTagsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        matcher = patch("services.synonym_service._active_synonym_matcher", build_synonym_matcher([
            {"standardTerm": "溶解氧", "terms": [{"term": "DO"}]},
        ]))
        matcher.start()
        self.addCleanup(matcher.stop)

    async def test_time_matches_survive_score_sort_threshold_and_top_n(self):
        records = [
            {"chunk_text": "年月低分", "tags": ["断面", "2021年", "3月"], "score": 0.65},
            {"chunk_text": "年月高分", "tags": ["断面", "2021年", "3月"], "score": 0.8},
            {"chunk_text": "同年其他月", "tags": ["断面", "2021年", "4月"], "score": 0.85},
            {"chunk_text": "其他年同月", "tags": ["断面", "2020年", "3月"], "score": 0.99},
            {"chunk_text": "时间未知", "score": 0.95},
            {"chunk_text": "阈值以下", "tags": ["2021年", "3月"], "score": 0.1},
        ]
        for tags, expected in (
            (["断面", "2021年", "3月"], ["年月高分", "年月低分", "同年其他月"]),
            (["断面", "2021年"], ["同年其他月", "年月高分", "年月低分"]),
            (["断面"], ["其他年同月", "时间未知", "同年其他月"]),
        ):
            with self.subTest(tags=tags):
                session = FakeSession(recall_response(records))
                backend = make_backend(session, FakeReranker())
                self.addAsyncCleanup(backend.close)
                result = await backend.search_all("断面报告", [("knowledge-1", 20, 3)], {"tags": tags})
                self.assertEqual([c.text for c in result], expected)
                self.assertEqual(session.calls[0]["json"]["tags"], tags)
                self.assertEqual(session.calls[0]["json"]["forceTagList"], [])
                self.assertEqual(backend.reranker.calls, [])

    async def test_missing_time_metadata_preserves_upstream_priority_and_filters_scores(self):
        session = FakeSession(recall_response([
            {"chunk_text": "低于阈值", "score": 0.1},
            {"chunk_text": "平台优先结果", "score": 0.65},
            {"chunk_text": "补召回高分", "score": 0.99},
        ]))
        backend = make_backend(session, FakeReranker())
        self.addAsyncCleanup(backend.close)
        result = await backend.search_all("2021年报告", [("knowledge-1", 20, 1)], {"tags": ["2021年"]})
        self.assertEqual([c.text for c in result], ["平台优先结果"])
        self.assertEqual(result[0].tags, ())

    def test_response_tags_are_optional_and_never_inferred(self):
        for raw, expected in (
            ([" 2021年 ", "3月", "3月", None, 1, ""], ("2021年", "3月")),
            (None, ()), ("2021年: 3月", ()), ({"year": "2021年"}, ()),
        ):
            with self.subTest(raw=raw):
                chunks = RagSearchBackend._parse_recall({
                    "code": 200, "data": {"records": [{"chunk_text": "2022年报告", "tags": raw}]},
                }, 20)
                self.assertEqual(chunks[0].tags, expected)

    def test_time_rules_apply_to_published_prompts_and_refresh_current_time(self):
        for current in ("2026-12-31T23:59:59+08:00", "2027-01-01T00:00:01+08:00"):
            with self.subTest(current=current), patch(
                "services.context_service.get_current_server_time", return_value=current,
            ):
                prompt = build_system_prompt((), system_prompt="已发布的自定义提示词")
                self.assertTrue(prompt.startswith("已发布的自定义提示词"))
                self.assertIn(current, prompt)
                self.assertIn("不把多个年份合并进一次 tags", prompt)
                self.assertIn("近N年", prompt)
                self.assertIn("requested_tags 只表示本次请求条件", prompt)

    async def test_five_year_calls_keep_provenance_and_continue_after_empty_or_failed_year(self):
        settings = get_settings().model_copy(deep=True)
        settings.business_scenarios["general"].rag.knowledge_bases = [
            RagKnowledgeBaseSettings(knowledge_id="knowledge-1", recall_top_n=20, top_n=2),
        ]
        old = {"chunk_text": "2022年报告", "fileName": "2022.pdf", "tags": ["2022年"], "score": 0.9}
        session = FakeSession(
            recall_response([old]), recall_response([]), RuntimeError("2024年查询失败"),
            recall_response([old]),
            recall_response([{"chunk_text": "2026年报告", "fileName": "2026.pdf", "score": 0.9}]),
        )
        backend = make_backend(session, FakeReranker())
        tool = RagSearchTool(backend)
        tools = ToolExecutor()
        tools.register("rag_search", "文档检索", tool.execute)
        tools.register_closer(tool.close)
        calls = [{"id": str(year), "name": "rag_search", "args": {
            "query": f"{year}年断面报告", "tags": ["断面", f"{year}年"],
        }} for year in range(2022, 2027)]
        llm = FakeLlm([([], calls), (["2022年依据<<SRC_1>>；2026年依据<<SRC_2>>"], [])])
        agent = AgentLoop(llm, tools)
        self.addAsyncCleanup(agent.close)
        with patch("agents.tools.rag_search.get_settings", return_value=settings), self.assertLogs(
            "agents.agent_loop", level="ERROR",
        ):
            events = [e async for e in agent.run_stream("近五年断面报告", [])]
        self.assertEqual([c["json"]["tags"] for c in session.calls],
                         [["断面", f"{year}年"] for year in range(2022, 2027)])
        finished = [e for e in events if e.type == "tool_finished"]
        self.assertEqual([e.success for e in finished], [True, True, False, True, True])
        results = {m["tool_call_id"]: json.loads(m["content"]) for m in llm.calls[1][0]
                   if m.get("name") == "rag_search" and m["tool_call_id"] != "2024"}
        self.assertEqual(results["2023"]["count"], 0)
        self.assertEqual(results["2025"]["requested_tags"], ["断面", "2025年"])
        self.assertEqual(results["2025"]["source_keys"], results["2022"]["source_keys"])
        self.assertEqual(results["2025"]["duplicate_count"], 1)
        material = next(m for m in llm.calls[1][0] if m.get("name") == "rag_buffer")
        chunks = json.loads(material["content"].split("\n", 1)[1])["chunks"]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["tags"], ["2022年"])
        self.assertEqual(chunks[1]["tags"], [])
        retrievals = [e.data for e in events if e.type == "retrieval"]
        self.assertEqual(retrievals[2]["requested_tags"], ["断面", "2025年"])
        self.assertEqual(retrievals[2]["chunks"][0]["tags"], ["2022年"])
        self.assertEqual("".join(e.content for e in events if e.type == "answer_delta"),
                         "2022年依据[[cite:1]]；2026年依据[[cite:2]]")
