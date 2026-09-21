from pathlib import Path
from unittest import TestCase
from unittest.mock import DEFAULT, patch

from services.token_budget_service import (
    ContextBudgetExceeded,
    DeepSeekV4TokenCounter,
    TokenBudgetService,
)


TOKENIZER_FILE = (
    Path(__file__).resolve().parents[1]
    / "resources/tokenizers/deepseek-v4/deepseek_v4_tokenizer/tokenizer.json"
)


class TokenBudgetServiceTest(TestCase):
    def setUp(self):
        self.counter = DeepSeekV4TokenCounter(TOKENIZER_FILE)
        # Fail a looping regression instead of hanging the test runner.
        counter_patch = patch.object(
            self.counter,
            "count_request",
            wraps=self.counter.count_request,
            side_effect=[DEFAULT] * 100 + [AssertionError("Token fitting did not terminate")],
        )
        counter_patch.start()
        self.addCleanup(counter_patch.stop)

    def test_official_tokenizer_counts_chinese_locally(self):
        self.assertEqual(self.counter.count_text("你好，世界"), 3)

    def test_drops_oldest_complete_exchange_before_current_question(self):
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "旧问题" * 80},
            {"role": "assistant", "content": "旧回答" * 80},
            {"role": "user", "content": "新问题"},
        ]
        full_count = self.counter.count_request(messages)
        service = TokenBudgetService(self.counter, full_count - 10)
        fitted = service.fit(
            messages,
            history_group_sizes=(2,),
            original_history_count=2,
            tools=None,
        )
        self.assertEqual([item["role"] for item in fitted.messages], ["system", "user"])
        self.assertEqual(fitted.messages[-1]["content"], "新问题")
        self.assertEqual(fitted.history_messages_dropped, 2)

    def test_truncates_tool_output_when_no_history_remains(self):
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "query", "args": {}}]},
            {"role": "tool", "content": "数据" * 5000},
        ]
        service = TokenBudgetService(self.counter, 300)
        fitted = service.fit(
            messages,
            history_group_sizes=(),
            original_history_count=0,
            tools=None,
        )
        self.assertLessEqual(fitted.estimated_input_tokens, 300)
        self.assertEqual(fitted.tool_results_truncated, 1)
        self.assertIn("工具结果因上下文长度限制已截断", fitted.messages[-1]["content"])

    def test_raises_when_minimal_tool_result_still_exceeds_budget(self):
        messages = [
            {"role": "system", "content": "规则" * 200},
            {"role": "user", "content": "问题"},
            {"role": "tool", "content": "数据" * 100},
        ]
        with self.assertRaises(ContextBudgetExceeded):
            TokenBudgetService(self.counter, 100).fit(
                messages,
                history_group_sizes=(),
                original_history_count=0,
                tools=None,
            )
        self.assertEqual(messages[-1]["content"], "数据" * 100)

    def test_continues_to_next_tool_result_when_first_truncation_is_insufficient(self):
        messages = [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "问题"},
            {"role": "tool", "content": "第一类数据" * 500},
            {"role": "tool", "content": "第二类数据" * 500},
        ]
        fitted = TokenBudgetService(self.counter, 150).fit(
            messages,
            history_group_sizes=(),
            original_history_count=0,
            tools=None,
        )
        self.assertLessEqual(fitted.estimated_input_tokens, 150)
        self.assertEqual(fitted.tool_results_truncated, 2)
        self.assertEqual(fitted.messages[:2], messages[:2])
        self.assertEqual(fitted.messages[2]["content"], "\n[工具结果因上下文长度限制已截断]")
        self.assertTrue(fitted.messages[3]["content"].startswith("第二类数据"))
        self.assertEqual(messages[2]["content"], "第一类数据" * 500)
        self.assertEqual(messages[3]["content"], "第二类数据" * 500)
