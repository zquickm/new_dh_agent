import unittest
from copy import deepcopy
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.skills import SkillSummary
from agents.tools.executor import ToolExecutor
from schemas.chat import ChatMessage, FrontendContext
from services.context_service import ContextService
from services.synonym_service import build_synonym_matcher
from tests.test_agent_loop import FakeLlm


class ContextServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_tool_history_is_atomic_under_message_and_token_limits(self):
        from services.token_budget_service import TokenBudgetService
        from tests.test_token_budget_service import TOKENIZER_FILE, DeepSeekV4TokenCounter

        history = [ChatMessage.model_validate(item) for item in [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "name": "load_skill", "args": {"name": "断面业务"}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "name": "load_skill", "content": "业务说明" * 500},
            {"role": "assistant", "content": "回答<<SRC_1>>[[cite:1]]"},
        ]]
        messages = self.service.build_messages(
            question="追问", history=history, frontend_context=None,
            skills=(), max_history_messages=2,
        )
        self.assertEqual(len(messages), 6)
        self.assertEqual(messages[-2]["content"], "回答")
        counter = DeepSeekV4TokenCounter(TOKENIZER_FILE)
        budget = counter.count_request([messages[0], messages[-1]]) + 10
        fitted = TokenBudgetService(counter, budget).fit(
            messages, history_group_sizes=(4,), original_history_count=4, tools=None,
        )
        self.assertEqual([m["role"] for m in fitted.messages], ["system", "user"])
        self.assertEqual(fitted.history_messages_dropped, 4)

    def setUp(self):
        matcher = build_synonym_matcher([
            {"standardTerm": "溶解氧", "terms": [{"term": "DO"}]},
        ])
        synonym_patch = patch(
            "services.synonym_service._active_synonym_matcher", matcher
        )
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)
        self.service = ContextService()
        self.context = FrontendContext.model_validate({
            "drawn_geometry": {
                "type": "Circle", "center": [121.5, 30.2], "radius": 5000,
            }
        })

    def test_history_limit_citations_and_request_isolation(self):
        history = [
            ChatMessage(role="user", content="旧问题"),
            ChatMessage(role="assistant", content="旧回答[[cite:1]]"),
            ChatMessage(role="user", content="上个问题"),
            ChatMessage(role="assistant", content="上个回答[[cite:2]]"),
        ]
        before = deepcopy(history)
        messages = self.service.build_messages(
            question="DO[[cite:3]]", history=history,
            frontend_context=self.context,
            skills=(SkillSummary(name="观测分析", description="查询观测站"),),
            max_history_messages=2,
        )
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "user"])
        self.assertIn("- 观测分析: 查询观测站", messages[0]["content"])
        self.assertEqual(messages[2]["content"], "上个回答")
        self.assertTrue(messages[-1]["content"].startswith("DO（溶解氧）"))
        self.assertIn('"center":[121.5,30.2]', messages[-1]["content"])
        self.assertNotIn("[[cite:", str(messages[1:]))
        self.assertEqual(history, before)
        other = self.service.build_messages(
            question="你好", history=[], frontend_context=None,
            skills=(), max_history_messages=2,
        )
        self.assertEqual(other[-1]["content"], "你好")
        self.assertEqual(len(other), 2)

    def test_round_constraints_do_not_accumulate_or_drop_tool_results(self):
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-1", "name": "query", "args": {}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "结果"},
        ]
        before = deepcopy(messages)
        first = self.service.build_round_messages(
            messages, round_number=1, max_rounds=2, is_forced_final_round=False,
        )
        final = self.service.build_round_messages(
            messages, round_number=2, max_rounds=2, is_forced_final_round=True,
        )
        self.assertEqual(messages, before)
        self.assertNotIn("本轮工具已禁用", first[0]["content"])
        self.assertIn("本轮工具已禁用", final[0]["content"])
        self.assertNotIn("第 1/2 轮", final[0]["content"])
        self.assertEqual(final[1:], messages[1:])

    async def test_agent_builds_map_and_follow_up_context(self):
        llm = FakeLlm([(["回答"], []), (["回答"], [])])
        agent = AgentLoop(llm, ToolExecutor())
        try:
            for question in ("这个区域的DO是多少", "该站点的经纬度是多少"):
                _ = [event async for event in agent.run_stream(
                    question,
                    [ChatMessage(role="assistant", content="小洋山站[[cite:1]]")],
                    frontend_context=self.context,
                )]
            initial = llm.calls[0][0]
            self.assertIn("DO（溶解氧）", initial[-1]["content"])
            self.assertIn('"query_type":"circle"', initial[-1]["content"])
            follow_up = llm.calls[1][0]
            self.assertNotIn("selected_area", follow_up[-1]["content"])
            self.assertIn("最近一轮明确讨论的单个站点", follow_up[-1]["content"])
            self.assertEqual(follow_up[1]["content"], "小洋山站")
            self.assertIn("当前是第 1/", follow_up[0]["content"])
        finally:
            await agent.close()
