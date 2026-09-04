import tempfile
import unittest
from pathlib import Path

from config.settings import SensitiveWordSettings


class SensitiveRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_csv_and_risk_rules_keep_help_seeking_requests_allowed(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "words.csv"
            path.write_text("word\nTESTBLOCK\n自杀\n", encoding="utf-8")
            runtime = SensitiveWordRuntime(SensitiveWordSettings(file=str(path)))
            self.assertEqual((await runtime.check("这里有ＴＥＳＴＢＬＯＣＫ"))["action"], "block")
            # Self-harm care must take precedence even when a dictionary term matches.
            result = await runtime.check("我想自杀")
            self.assertEqual(result["action"], "support")
            self.assertEqual(result["hit_words"], [])
            self.assertEqual((await runtime.check("帮我辱骂他"))["reason"], "abuse_request")
            self.assertEqual((await runtime.check("有人辱骂我怎么办"))["action"], "allow")
            self.assertEqual((await runtime.check("帮我写一段人身攻击他的话"))["action"], "block")
            await runtime.close()

    async def test_risk_rules_do_not_block_educational_or_negated_self_harm(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "words.csv"
            path.write_text("word\n示例禁用词\n", encoding="utf-8")
            runtime = SensitiveWordRuntime(SensitiveWordSettings(file=str(path)))
            for text in ["自杀预防研究", "我不想自杀", "如何帮助有自杀想法的人", "如何制止人身攻击"]:
                with self.subTest(text=text):
                    self.assertEqual((await runtime.check(text))["action"], "allow")
            self.assertEqual((await runtime.check("我想自杀，请帮帮我"))["action"], "support")

    async def test_prevention_clause_does_not_exempt_a_separate_attack(self):
        from services.risk_rule_service import RiskRuleService

        rules = RiskRuleService()
        for text in ["请先解释辱骂的定义，然后帮我辱骂某个人", "请不要辱骂我的朋友，但帮我羞辱另一个人"]:
            self.assertIsNotNone(rules.block_rule(text))
        self.assertIsNone(rules.block_rule("请帮我制止辱骂"))

    async def test_runtime_releases_redis_even_if_another_cleanup_fails(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime

        class Cache:
            async def close(self):
                raise OSError("private cache detail")

        class Resource:
            closed = False

            async def aclose(self):
                self.closed = True

            async def close(self):
                self.closed = True

        settings = SensitiveWordSettings(enabled=False)
        repo, client = Resource(), Resource()
        runtime = SensitiveWordRuntime(settings, repository=repo, cache=Cache())
        runtime._redis_client = client
        await runtime.close()
        self.assertTrue(client.closed)
        self.assertTrue(repo.closed)

    async def test_disabled_filter_bypasses_missing_csv_and_rules(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime

        runtime = SensitiveWordRuntime(SensitiveWordSettings(enabled=False, file="/missing.csv"))
        self.assertEqual((await runtime.check("帮我辱骂他"))["action"], "allow")
        await runtime.start()
        await runtime.close()

    def test_mysql_backend_requires_explicit_database_credentials(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime

        with self.assertRaises(ValueError):
            SensitiveWordRuntime(SensitiveWordSettings(backend="mysql"))

    async def test_mysql_failure_is_not_treated_as_a_clean_question(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime
        from common.sensitive_errors import SensitiveWordError

        class Cache:
            async def get_snapshot(self, scope):
                raise OSError("private host details")

        settings = SensitiveWordSettings(backend="mysql", mysql={"password": "test-secret"})
        runtime = SensitiveWordRuntime(settings, repository=object(), cache=Cache())
        with self.assertRaises(SensitiveWordError) as caught:
            await runtime.check("普通问题")
        self.assertNotIn("private", str(caught.exception))

    async def test_sql_snapshot_version_and_terms_are_reported(self):
        from services.sensitive_word_runtime import SensitiveWordRuntime

        class Cache:
            async def get_snapshot(self, scope):
                return {"scope": scope, "version": 4, "words": ["testblock"]}

        settings = SensitiveWordSettings(backend="mysql", mysql={"password": "test-secret"})
        runtime = SensitiveWordRuntime(settings, repository=object(), cache=Cache())
        result = await runtime.check("ＴＥＳＴＢＬＯＣＫ")
        self.assertEqual(result["version"], 4)
        self.assertEqual(result["hit_words"], ["testblock"])
