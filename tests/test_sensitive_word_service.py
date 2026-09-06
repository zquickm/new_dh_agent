import unittest

from services.sensitive_word_service import (
    SensitiveWordConfigurationError,
    SensitiveWordMatcher,
)


class SensitiveWordMatcherTest(unittest.TestCase):
    def test_matches_normalized_input_rules_and_ignores_duplicates(self):
        matcher = SensitiveWordMatcher(
            [
                {"word": "测试敏感词一", "applyScope": "CHAT_INPUT"},
                {"word": " TESTBLOCK ", "applyScope": "BOTH"},
                {"word": "testblock", "applyScope": "CHAT_INPUT"},
            ]
        )

        self.assertTrue(matcher.matches("这里包含测试敏感词一，请拦截"))
        self.assertTrue(matcher.matches("这里包含ＴＥＳＴＢＬＯＣＫ"))
        self.assertFalse(matcher.matches("这是一条普通问题"))

    def test_current_matcher_does_not_distinguish_rule_scope(self):
        matcher = SensitiveWordMatcher(
            [
                {"word": "仅输出检测", "applyScope": "CHAT_OUTPUT"},
                {"word": "输入检测", "applyScope": "CHAT_INPUT"},
            ]
        )

        self.assertTrue(matcher.matches("仅输出检测"))
        self.assertTrue(matcher.matches("输入检测"))

    def test_rejects_snapshot_without_input_rules(self):
        for rules in (
            [],
            [{"word": ""}],
        ):
            with self.subTest(rules=rules):
                with self.assertRaises(SensitiveWordConfigurationError):
                    SensitiveWordMatcher(rules)


if __name__ == "__main__":
    unittest.main()
