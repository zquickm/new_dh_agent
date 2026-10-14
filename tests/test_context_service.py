import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.skills import SkillSummary
from agents.tools.executor import ToolExecutor
from schemas.chat import ChatMessage, FrontendContext
from services.context_service import ContextService, OVERVIEW_DATA_PROMPT, build_system_prompt
from services.synonym_service import build_synonym_matcher
from tests.test_agent_loop import FakeLlm


class ContextServiceTest(unittest.IsolatedAsyncioTestCase):
    def test_overview_rules_apply_to_published_prompts_and_submodule_overrides(self):
        for scenario in ("general", "海洋断面数据分析"):
            prompt = build_system_prompt((), "旧发布提示：统计数据查询数据库", rag_scenario=scenario)
            self.assertTrue(prompt.startswith("旧发布提示：统计数据查询数据库"))
            self.assertEqual(prompt.count(OVERVIEW_DATA_PROMPT), 1)
            self.assertIn('operation="island_overview"', prompt)
            self.assertIn('operation="dm_duanmian_overview"', prompt)
            self.assertIn("优先于已有提示词、Skill 和历史", prompt)
            self.assertIn("接口未覆盖的历史要素、跨期指标统计", prompt)

    def test_each_request_refreshes_time_with_custom_system_prompt(self):
        timestamps = ["2026-09-20T23:59:59+08:00", "2026-09-21T00:00:01+08:00"]
        with patch(
            "agents.tools.current_server_time.datetime"
        ) as clock:
            current = clock.now.return_value.astimezone.return_value
            current.isoformat.side_effect = timestamps
            current.tzname.return_value = "CST"
            current.strftime.return_value = "+0800"
            prompts = [self.service.build_messages(
                question="最近的报告", history=[], frontend_context=None,
                skills=(), max_history_messages=2, system_prompt="自定义系统提示",
            )[0]["content"] for _ in timestamps]

        self.assertEqual(clock.now.call_count, 2)
        for timestamp, prompt in zip(timestamps, prompts):
            self.assertTrue(prompt.startswith("自定义系统提示\n\n"))
            self.assertIn(timestamp, prompt)
            self.assertIn('"timezone": "CST"', prompt)
            self.assertIn('"utc_offset": "+0800"', prompt)
            self.assertIn("用户指定历史时间范围时，以该范围为准", prompt)
        self.assertNotIn(timestamps[0], prompts[1])

    def test_historical_rag_results_keep_only_status_without_mutation(self):
        for result, expected in (
            ({"chunks": [{"text": "旧片段"}]}, True),
            ({"chunks": []}, True),
            ({"success": True, "buffer_updated": True, "buffer_ids": [19]}, True),
            ({"success": False, "error": "包含旧片段的错误"}, False),
            ("无效结果", False),
        ):
            with self.subTest(result=result):
                history = [ChatMessage.model_validate(item) for item in [
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "rag", "function": {"name": "rag_search", "arguments": "{}"}},
                        {"id": "db", "name": "database_query", "args": {}},
                    ]},
                    {"role": "tool", "tool_call_id": "rag", "content": json.dumps(result)},
                    {"role": "tool", "tool_call_id": "db", "name": "database_query", "content": "数据库结果"},
                ]]
                before = deepcopy(history)
                filtered = ContextService.filter_rag_history(history)
                self.assertEqual(filtered[0], history[0])
                self.assertEqual(json.loads(filtered[1].content), {"success": expected})
                self.assertEqual(filtered[2], history[2])
                self.assertEqual(history, before)
                self.assertEqual(ContextService.filter_rag_history(filtered), filtered)

    def test_skill_history_filters_only_disabled_calls_without_mutation(self):
        from langchain_core.messages import convert_to_messages

        history = [ChatMessage.model_validate(item) for item in [
            {"role": "user", "content": "断面业务和空间联动都使用过"},
            {"role": "assistant", "content": "分别处理", "tool_calls": [
                {"id": "disabled", "name": "load_skill", "args": {"name": "断面业务"}},
                {"id": "enabled", "name": "load_skill", "args": {"name": "空间联动"}},
                {"id": "business", "name": "database_query", "args": {"query": "数据"}},
            ]},
            {"role": "tool", "tool_call_id": "disabled", "content": "断面完整说明"},
            {"role": "tool", "tool_call_id": "enabled", "name": "load_skill", "content": "空间完整说明"},
            {"role": "tool", "tool_call_id": "business", "name": "database_query", "content": "业务数据"},
            {"role": "assistant", "content": "正常历史回答保留"},
        ]]
        before = deepcopy(history)
        filtered = self.service.filter_skill_history(history, frozenset({"空间联动"}))
        self.assertEqual([call["id"] for call in filtered[1].tool_calls], ["enabled", "business"])
        self.assertEqual([item.content for item in filtered], [
            "断面业务和空间联动都使用过", "分别处理", "空间完整说明", "业务数据", "正常历史回答保留",
        ])
        convert_to_messages([item.model_dump(exclude_none=True) for item in filtered])
        self.assertEqual(history, before)
        filtered[1].tool_calls[0]["args"]["name"] = "修改副本"
        self.assertEqual(history, before)

    def test_skill_history_reused_ids_are_matched_in_their_own_blocks(self):
        history = [ChatMessage.model_validate(item) for item in [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "reused", "name": "load_skill", "args": {"name": "断面业务"}},
            ]},
            {"role": "tool", "tool_call_id": "reused", "content": "禁用说明"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": "下一任务"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "reused", "name": "load_skill", "args": {"name": "空间联动"}},
            ]},
            {"role": "tool", "tool_call_id": "reused", "name": "load_skill", "content": "启用说明"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "reused", "name": "database_query", "args": {}},
            ]},
            {"role": "tool", "tool_call_id": "reused", "content": "业务结果"},
        ]]
        filtered = self.service.filter_skill_history(history, frozenset({"空间联动"}))
        self.assertEqual([item.content for item in filtered], [
            "旧回答", "下一任务", "", "启用说明", "", "业务结果",
        ])

    def test_skill_history_normalizes_legacy_calls(self):
        from langchain_core.messages import convert_to_messages

        history = [ChatMessage.model_validate(item) for item in [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "disabled", "type": "function", "function": {
                    "name": "load_skill", "arguments": '{"name":"断面业务"}',
                }},
                {"id": "enabled", "type": "function", "function": {
                    "name": "load_skill", "arguments": '{"name":"空间联动"}',
                }},
                {"id": "business", "name": "database_query", "args": '{"query":"保留"}'},
            ]},
            {"role": "tool", "tool_call_id": "disabled", "name": "load_skill", "content": "禁用说明"},
            {"role": "tool", "tool_call_id": "enabled", "content": "启用说明"},
            {"role": "tool", "tool_call_id": "business", "content": "业务结果"},
        ]]
        filtered = self.service.filter_skill_history(history, frozenset({"空间联动"}))
        self.assertEqual(filtered[0].tool_calls, [
            {"id": "enabled", "name": "load_skill", "args": {"name": "空间联动"}, "type": "tool_call"},
            {"id": "business", "name": "database_query", "args": {"query": "保留"}, "type": "tool_call"},
        ])
        convert_to_messages([item.model_dump(exclude_none=True) for item in filtered])
        self.assertEqual([item.content for item in filtered], ["", "启用说明", "业务结果"])

    def test_unrecognized_and_orphan_skill_results_are_not_replayed(self):
        for args in ({"name": "不存在"}, {}, {"name": []}, "无效参数", "[]"):
            with self.subTest(args=args):
                history = [
                    ChatMessage(role="tool", name="load_skill", tool_call_id="orphan", content="孤立说明"),
                    ChatMessage(role="assistant", content="保留正文", tool_calls=[
                        {"id": "call", "name": "load_skill", "args": args},
                    ]),
                    ChatMessage(role="tool", tool_call_id="call", content="无法确认的说明"),
                    ChatMessage(role="tool", name="database_query", tool_call_id="business", content="业务结果"),
                ]
                filtered = self.service.filter_skill_history(history, frozenset({"空间联动"}))
                self.assertEqual([item.content for item in filtered], ["保留正文", "业务结果"])
                self.assertIsNone(filtered[0].tool_calls)

    def test_current_skill_list_overrides_old_loading_instructions(self):
        prompt = build_system_prompt((), "断面业务任务必须先加载断面业务")
        self.assertIn("当前可用能力：\n- （无）", prompt)
        self.assertIn("仅在该 Skill 出现在本次列表中时适用", prompt)
        self.assertTrue(prompt.endswith("仍可根据任务需要正常调用业务工具。"))

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
