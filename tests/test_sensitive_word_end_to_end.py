"""Opt-in tests against disposable local MySQL and Redis, never business databases."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import asyncmy
import httpx

from config.settings import SensitiveWordSettings, get_settings
from services.sensitive_word_runtime import SensitiveWordRuntime


@unittest.skipUnless(
    os.environ.get("SENSITIVE_TEST_MYSQL_PORT") and os.environ.get("SENSITIVE_TEST_REDIS_URL"),
    "requires isolated MySQL and Redis test endpoints",
)
class SensitiveWordEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.database = "sensitive_e2e_" + uuid4().hex
        self.admin_connection = await asyncmy.connect(
            host="127.0.0.1", port=int(os.environ["SENSITIVE_TEST_MYSQL_PORT"]),
            user="root", password="isolated-test-password", autocommit=True,
        )
        async with self.admin_connection.cursor() as cursor:
            await cursor.execute("CREATE DATABASE " + self.database + " CHARACTER SET utf8mb4")
            await cursor.execute("USE " + self.database)
            for statement in (Path(__file__).parents[1] / "migrations/001_sensitive_words.sql").read_text().split(";"):
                if statement.strip():
                    await cursor.execute(statement)
        self.settings = SensitiveWordSettings(
            backend="mysql", admin_token="isolated-management-test-token",
            mysql={"host": "127.0.0.1", "port": int(os.environ["SENSITIVE_TEST_MYSQL_PORT"]),
                   "user": "root", "password": "isolated-test-password", "database": self.database},
            redis={"url": os.environ["SENSITIVE_TEST_REDIS_URL"], "key_prefix": self.database},
        )
        self.runtime = SensitiveWordRuntime(self.settings)
        await self.runtime.start()

    async def asyncTearDown(self):
        if self.runtime._redis_client:
            await self.runtime._redis_client.delete(self.database + ":agent")
        await self.runtime.close()
        async with self.admin_connection.cursor() as cursor:
            await cursor.execute("DROP DATABASE " + self.database)
        self.admin_connection.close()

    async def test_real_management_chat_cross_worker_and_publication_recovery(self):
        from controller.sensitive_word_controller import get_admin_service
        from main import app
        from repositories.memory_conversation_repository import conversation_repository
        from services.chat_service import chat_service
        from services.sensitive_word_admin_service import SensitiveWordAdminService

        previous_overrides = dict(app.dependency_overrides)
        previous_runtime, previous_agent = chat_service._safety_runtime, chat_service._agent
        app_settings = get_settings()
        app.dependency_overrides[get_settings] = lambda: app_settings.model_copy(update={"sensitive_words": self.settings})
        app.dependency_overrides[get_admin_service] = lambda: SensitiveWordAdminService(self.runtime)
        chat_service._safety_runtime, chat_service._agent = self.runtime, None
        headers = {app_settings.internal_request.header_name: app_settings.internal_request.token.get_secret_value(),
                   "X-DH-Admin-Token": self.settings.admin_token.get_secret_value(),
                   "X-User-Id": "101", "X-Auth-Source": "dh-gateway"}
        second = SensitiveWordRuntime(self.settings)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                prefix = "/internal/sensitive/agent"
                self.assertEqual((await client.get(prefix + "/categories")).status_code, 403)
                wrong = {**headers, "X-DH-Admin-Token": "wrong"}
                self.assertEqual((await client.get(prefix + "/categories", headers=wrong)).status_code, 403)
                self.assertEqual((await client.get("/internal/sensitive/forum/categories", headers=headers)).status_code, 400)
                created = await client.post(prefix + "/categories", headers=headers, json={"name": "接口测试"})
                self.assertEqual(created.status_code, 200, created.text)
                category_id = created.json()["data"]["item"]["id"]
                created = await client.post(prefix + "/words", headers=headers,
                                            json={"category_id": category_id, "word": "TESTBLOCK"})
                self.assertEqual(created.status_code, 200, created.text)
                word_id = created.json()["data"]["item"]["id"]
                await second.start()
                self.assertEqual((await second.check("ＴＥＳＴＢＬＯＣＫ"))["version"], 2)
                check = await client.post(prefix + "/check", headers=headers, json={"text": "ＴＥＳＴＢＬＯＣＫ"})
                self.assertEqual(check.json()["data"]["action"], "block")
                with patch("services.chat_service.build_agent_loop", side_effect=AssertionError("must not build Agent")):
                    response = await client.post("/agent/chat/stream", headers=headers, json={"question": "ＴＥＳＴＢＬＯＣＫ"})
                self.assertEqual(response.status_code, 200)
                frames = response.text.strip().split("\n\n")
                self.assertEqual([frame.splitlines()[0] for frame in frames], ["event: conversation", "event: token", "event: done"])
                payload = json.loads(frames[-1].splitlines()[1][6:])["data"]
                self.assertIsNone(payload["assistant_message_id"])
                detail = await conversation_repository.get(payload["conversation_id"], user_id="101")
                self.assertEqual(detail.messages, [])
                self.assertIsNone(await conversation_repository.get(payload["conversation_id"], user_id="202"))
                self.assertIsNone(chat_service._agent)

                async def publication_outage(*_args, **_kwargs):
                    raise OSError("simulated Redis outage")

                with patch.object(self.runtime.cache.redis, "eval", publication_outage):
                    failed = await client.patch(prefix + f"/words/{word_id}", headers=headers, json={"word": "NEWBLOCK"})
                self.assertEqual(failed.status_code, 503)
                self.assertEqual(failed.json()["data"], {"persisted": True, "refreshed": False, "version": 3})
                self.assertEqual((await self.runtime.repository.load_snapshot("agent"))["words"], ["newblock"])
                self.assertEqual((await second.check("TESTBLOCK"))["action"], "block")
                refreshed = await client.post(prefix + "/refresh", headers=headers)
                self.assertEqual(refreshed.json()["data"], {"version": 3, "count": 1})
                self.assertEqual((await second.check("NEWBLOCK"))["action"], "block")
                self.assertEqual((await second.check("TESTBLOCK"))["action"], "allow")
                disabled = await client.patch(prefix + f"/categories/{category_id}", headers=headers, json={"status": 0})
                self.assertEqual(disabled.status_code, 200)
                self.assertEqual((await second.check("NEWBLOCK"))["action"], "allow")
        finally:
            await second.close()
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)
            chat_service._safety_runtime, chat_service._agent = previous_runtime, previous_agent

    async def test_real_cli_preflight_apply_and_repeat_preserve_local_edits(self):
        snapshot = {"schema_version": 1, "source_system": "fixture", "scope": "agent",
                    "exported_at": "2026-09-04T00:00:00+00:00",
                    "categories": [{"source_id": "cat-1", "name": "源分类", "status": 1}],
                    "words": [{"source_id": "word-1", "category_source_id": "cat-1", "word": "原始词", "status": 1},
                              {"source_id": "word-2", "category_source_id": "cat-1", "word": "禁用词", "status": 0}]}
        env = {**os.environ, "NEW_DH_SENSITIVE_WORDS__BACKEND": "mysql",
               "NEW_DH_SENSITIVE_WORDS__MYSQL__HOST": "127.0.0.1",
               "NEW_DH_SENSITIVE_WORDS__MYSQL__PORT": os.environ["SENSITIVE_TEST_MYSQL_PORT"],
               "NEW_DH_SENSITIVE_WORDS__MYSQL__USER": "root",
               "NEW_DH_SENSITIVE_WORDS__MYSQL__PASSWORD": "isolated-test-password",
               "NEW_DH_SENSITIVE_WORDS__MYSQL__DATABASE": self.database,
               "NEW_DH_SENSITIVE_WORDS__REDIS__URL": os.environ["SENSITIVE_TEST_REDIS_URL"],
               "NEW_DH_SENSITIVE_WORDS__REDIS__KEY_PREFIX": self.database}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")

            async def run_cli(*extra):
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "scripts/import_sensitive_words.py", "--input", str(path), *extra,
                    cwd=Path(__file__).parents[1], env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                self.assertEqual(process.returncode, 0, stderr.decode())
                return json.loads(stdout)

            preview = await run_cli()
            self.assertFalse(preview["persisted"])
            self.assertEqual(preview["created_words"], 2)
            self.assertEqual((await self.runtime.repository.list_words("agent"))["total"], 0)
            applied = await run_cli("--apply")
            self.assertTrue(applied["persisted"])
            self.assertEqual((await self.runtime.check("原始词"))["action"], "block")
            self.assertEqual((await self.runtime.check("禁用词"))["action"], "allow")
            word = (await self.runtime.repository.list_words("agent", word="原始词"))["records"][0]
            await self.runtime.repository.update_word("agent", word["id"], {"word": "本地编辑"})
            repeated = await run_cli("--apply")
            self.assertEqual(repeated["skipped_words"], 2)
            self.assertEqual((await self.runtime.check("本地编辑"))["action"], "block")
            self.assertEqual((await self.runtime.check("原始词"))["action"], "allow")
