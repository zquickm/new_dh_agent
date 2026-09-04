import tempfile
import unittest
from pathlib import Path

from services.sensitive_word_service import (
    SensitiveWordConfigurationError,
    SensitiveWordMatcher,
)


class SensitiveWordMatcherTest(unittest.TestCase):
    def _write_csv(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8-sig")

    def test_matches_normalized_substrings_and_ignores_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensitive_words.csv"
            self._write_csv(
                path,
                "word\n测试敏感词一\n TESTBLOCK \ntestblock\n\n",
            )
            matcher = SensitiveWordMatcher(path)

            self.assertTrue(matcher.matches("这里包含测试敏感词一，请拦截"))
            self.assertTrue(matcher.matches("这里包含ＴＥＳＴＢＬＯＣＫ"))
            self.assertFalse(matcher.matches("这是一条普通问题"))

    def test_reloads_valid_csv_changes_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensitive_words.csv"
            self._write_csv(path, "word\n旧词\n")
            matcher = SensitiveWordMatcher(path)

            self.assertTrue(matcher.matches("包含旧词"))
            self._write_csv(path, "word\n全新敏感词\n")

            self.assertFalse(matcher.matches("包含旧词"))
            self.assertTrue(matcher.matches("包含全新敏感词"))

    def test_keeps_last_valid_words_when_hot_reload_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sensitive_words.csv"
            self._write_csv(path, "word\n旧敏感词\n")
            matcher = SensitiveWordMatcher(path)

            self._write_csv(path, "unexpected\n新词\n")

            self.assertTrue(matcher.matches("仍然包含旧敏感词"))
            self.assertFalse(matcher.matches("只包含新词"))

    def test_rejects_invalid_initial_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.csv"
            wrong_header = root / "wrong-header.csv"
            empty = root / "empty.csv"
            self._write_csv(wrong_header, "unexpected\n词条\n")
            self._write_csv(empty, "word\n\n")

            for path in (missing, wrong_header, empty):
                with self.subTest(path=path.name):
                    with self.assertRaises(SensitiveWordConfigurationError):
                        SensitiveWordMatcher(path)


if __name__ == "__main__":
    unittest.main()
