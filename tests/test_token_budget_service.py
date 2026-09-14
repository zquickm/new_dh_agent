from pathlib import Path
from unittest import TestCase

from services.token_budget_service import DeepSeekV4TokenCounter, TokenBudgetService


TOKENIZER_FILE = (
    Path(__file__).resolve().parents[1]
    / "resources/tokenizers/deepseek-v4/deepseek_v4_tokenizer/tokenizer.json"
)


class TokenBudgetServiceTest(TestCase):
    def setUp(self):
        self.counter = DeepSeekV4TokenCounter(TOKENIZER_FILE)

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
