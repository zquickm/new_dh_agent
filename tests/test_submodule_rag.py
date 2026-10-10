import asyncio
import unittest
from unittest.mock import patch

from pydantic import SecretStr, ValidationError

from agents.agent_loop import AgentLoop
from agents.tools.context import ToolContext
from agents.tools.registry import build_skill_catalog, build_tool_executor
from config.settings import RagKnowledgeBaseSettings, get_settings
from schemas.chat import AgentRequest, FrontContext
from services.chat_service import ChatService
from services.context_service import ContextService
from services.business_scenario import resolve_rag_scenario
from services.synonym_service import build_synonym_matcher
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm
from tests.test_rag_reranker import FakeResponse, FakeSession


class SubmoduleRagTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = get_settings().model_copy(deep=True)
        rag = self.settings.tools.rag
        rag.api_token = SecretStr("test-token")
        rag.rerank.enabled = False
        self.settings.business_scenarios.general.prompt = "通用专属提示"
        self.settings.business_scenarios.xiamen.prompt = "厦门专属提示"
        self.settings.business_scenarios.三新技术适用评估.prompt = "三新专属提示"
        self.settings.business_scenarios.三新技术适用评估.rag.score = 0.85
        self.settings.business_scenarios.三新技术适用评估.rag.result_score_threshold = 0.8
        self.settings.business_scenarios.三新技术适用评估.rag.knowledge_bases = [
            RagKnowledgeBaseSettings(knowledge_id="sanxin-only", recall_top_n=4, top_n=2),
        ]
        for target in ("agents.agent_loop.get_settings", "agents.tools.rag_search.get_settings",
                       "services.context_service.get_settings"):
            mock = patch(target, return_value=self.settings)
            mock.start()
            self.addCleanup(mock.stop)
        matcher = patch("services.synonym_service._active_synonym_matcher", build_synonym_matcher([
            {"standardTerm": "厦门", "terms": [{"term": "鹭岛"}]},
        ]))
        matcher.start()
        self.addCleanup(matcher.stop)
        self.catalog = build_skill_catalog()
        self.tools = build_tool_executor(self.catalog)
        self.addAsyncCleanup(self.tools.close)
        self.rag = self.tools.get("rag_search").function.__self__
        self.session = FakeSession(FakeResponse({"code": 200, "data": {"records": [
            {"chunk_text": f"片段{score}", "fileName": f"资料{score}.pdf", "score": score}
            for score in (0.1, 0.3, 0.7, 0.9)
        ]}}))
        self.rag.backend._session = self.session

    def test_front_context_only_accepts_board_names(self):
        request = AgentRequest.model_validate({
            "question": "请查询",
            "front_context": {"module_l1": "海政协同赋能", "module_l2": " 三新技术适用评估 ",
                              "prompt": "客户端不能注入此提示", "rag_scenario": "xiamen"},
        })
        self.assertEqual(request.front_context.module_l2, "三新技术适用评估")
        self.assertEqual(set(request.front_context.model_dump()), {"module_l1", "module_l2"})
        self.assertEqual(resolve_rag_scenario(request.front_context), "三新技术适用评估")
        for context in (None, FrontContext(), FrontContext(module_l2="未知"), FrontContext(module_l2="厦门"),
                        FrontContext(module_l1="三新技术适用评估", module_l2="政策协同分析评估")):
            self.assertEqual(resolve_rag_scenario(context), "general")
        for value in ([], {"module_l2": "长" * 101}, {"module_l2": {"prompt": "注入"}}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AgentRequest(question="查询", front_context=value)

    def test_prompt_rebuilds_use_the_current_scenario_without_accumulating(self):
        context = ContextService()
        for scenario, question, wanted in (
            ("三新技术适用评估", "查询", "三新专属提示"),
            ("三新技术适用评估", "查询", "三新专属提示"),
            ("三新技术适用评估", "厦门业务", "厦门专属提示"),
            ("三新技术适用评估", "鹭岛业务", "厦门专属提示"),
            ("general", "疑点疑区", "厦门专属提示"),
            ("general", "图斑", "厦门专属提示"),
            ("general", "查询", "通用专属提示"),
        ):
            prompt = context.build_messages(
                question=question, history=[], frontend_context=None, skills=(),
                max_history_messages=0, system_prompt="基础提示词", rag_scenario=scenario,
            )[0]["content"]
            self.assertTrue(prompt.startswith("基础提示词\n\n"))
            self.assertEqual(prompt.count(wanted), 1)
            for unwanted in {"通用专属提示", "三新专属提示", "厦门专属提示"} - {wanted}:
                self.assertNotIn(unwanted, prompt)

    async def test_chat_switches_board_prompts_and_rag_without_changing_xiamen_exception(self):
        llm = FakeLlm([])
        service = ChatService()
        service._agent = AgentLoop(llm, self.tools, self.catalog)
        self.addAsyncCleanup(service.close)
        conversation_id = None
        cases = [
            ("政策协同分析评估", "业务资料", "general", 9, "东溟知识库检索"),
            ("三新技术适用评估", "业务资料", "三新技术适用评估", 1, "东溟知识库检索"),
            ("政策协同分析评估", "业务资料", "general", 9, "东溟知识库检索"),
            ("未知板块", "业务资料", "general", 9, "东溟知识库检索"),
            (None, "业务资料", "general", 9, "东溟知识库检索"),
            *[("三新技术适用评估", query, "xiamen", 12, "厦门局业务知识库")
              for query in ("厦门资料", "疑点疑区", "图斑", "鹭岛资料")],
        ]
        repository = MemoryConversationRepository()
        with patch("services.chat_service.conversation_repository", repository), patch(
            "services.chat_service.generate_recommendations", return_value=[],
        ):
            for board, query, expected, count, display_name in cases:
                with self.subTest(board=board, query=query):
                    llm.responses = iter([
                        ([], [{"id": str(i), "name": "rag_search", "args": {"query": query}}])
                        for i in range(2)
                    ] + [(["完成"], [])])
                    llm.calls.clear()
                    before = len(self.session.calls)
                    request = AgentRequest(
                        question=query, conversation_id=conversation_id,
                        front_context=FrontContext(module_l1="海政协同赋能", module_l2=board) if board is not None else None,
                    )
                    with self.assertLogs(level="INFO") as logs:
                        events = [event async for event in service.stream_chat(request, user_id="101")]
                    front_context = request.front_context.model_dump() if request.front_context else None
                    base_scenario = resolve_rag_scenario(request.front_context)
                    self.assertTrue(any(
                        f"front_context={front_context!r} base_rag_scenario={base_scenario}" in line
                        for line in logs.output
                    ))
                    self.assertTrue(any(f"prompt_scenario={expected}" in line for line in logs.output))
                    self.assertTrue(any(f"rag_scenario={expected} query=" in line for line in logs.output))
                    self.assertFalse(any(e["event"] == "error" for e in events))
                    conversation_id = events[0]["data"]["conversation_id"]
                    business_scenario = getattr(self.settings.business_scenarios, expected)
                    scenario = business_scenario.rag
                    payloads = [c["json"] for c in self.session.calls[before:]]
                    self.assertEqual([p["knowledge_ids"] for p in payloads],
                                     [[b.knowledge_id] for b in scenario.knowledge_bases] * 2)
                    self.assertEqual([p["top_n"] for p in payloads],
                                     [b.recall_top_n for b in scenario.knowledge_bases] * 2)
                    self.assertTrue(all(p["score"] == scenario.score for p in payloads))
                    retrievals = [e["data"] for e in events if e["event"] == "retrieval"]
                    self.assertEqual([r["result_count"] for r in retrievals], [count, count])
                    self.assertTrue(all(r["tool_name"] == display_name for r in retrievals))
                    processes = [e["data"] for e in events if e["event"] == "process"
                                 and e["data"].get("step_id") in {"0", "1"}]
                    self.assertEqual(len(processes), 4)
                    self.assertTrue(all(p["title"] == f"算法工具箱：{display_name}" for p in processes))
                    self.assertEqual([p["summary"] for p in processes],
                                     [f"{display_name}正在执行", f"{display_name}执行完成"] * 2)
                    wanted = business_scenario.prompt.strip()
                    for messages, _ in llm.calls:
                        self.assertEqual(messages[0]["content"].count(wanted), 1)
                        for unwanted in {"通用专属提示", "三新专属提示", "厦门专属提示"} - {wanted}:
                            self.assertNotIn(unwanted, messages[0]["content"])

    async def test_prompt_and_retrieval_select_scenarios_from_their_own_input(self):
        for question, query, prompt_scene, retrieval_scene in (
            ("鹭岛业务", "技术资料", "xiamen", "三新技术适用评估"),
            ("技术适用条件", "鹭岛资料", "三新技术适用评估", "xiamen"),
        ):
            with self.subTest(question=question, query=query):
                prompt = ContextService().build_messages(
                    question=question, history=[], frontend_context=None, skills=(),
                    max_history_messages=0, rag_scenario="三新技术适用评估",
                )[0]["content"]
                self.assertIn(getattr(self.settings.business_scenarios, prompt_scene).prompt, prompt)
                self.assertNotIn(getattr(self.settings.business_scenarios, retrieval_scene).prompt, prompt)
                before = len(self.session.calls)
                await self.rag.execute(query, context=ToolContext(rag_scenario="三新技术适用评估"))
                scenario = getattr(self.settings.business_scenarios, retrieval_scene).rag
                self.assertEqual(
                    [call["json"]["knowledge_ids"] for call in self.session.calls[before:]],
                    [[library.knowledge_id] for library in scenario.knowledge_bases],
                )

    async def test_concurrent_tool_contexts_do_not_override_each_other_or_accept_model_context(self):
        schema = self.tools.get("rag_search").parameters
        self.assertEqual(set(schema["properties"]), {"query", "tags"})
        before = len(self.session.calls)
        with self.assertRaisesRegex(ValueError, "上下文不能由模型提供"):
            await self.tools.execute("rag_search", {"query": "主题", "context": {"rag_scenario": "xiamen"}})
        self.assertEqual(len(self.session.calls), before)
        general, sanxin = await asyncio.gather(
            self.rag.execute("同一问题", context=ToolContext(rag_scenario="general")),
            self.rag.execute("同一问题", context=ToolContext(rag_scenario="三新技术适用评估")),
        )
        self.assertEqual((general["count"], sanxin["count"]), (9, 1))
        self.assertEqual((self.rag.backend.score, self.rag.backend.result_score_threshold), (0.6, 0.3))
