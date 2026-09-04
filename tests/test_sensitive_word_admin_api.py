from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import SecretStr

from common.json_result import JsonResult
from common.sensitive_errors import SensitiveWordError
from config.settings import get_settings
from controller.sensitive_word_controller import get_admin_service, router
from middleware.internal_request_auth import InternalRequestAuthMiddleware
from services.sensitive_word_admin_service import SensitiveWordAdminService


class MemoryRepository:
    """Stateful replacement for the external MySQL boundary only."""

    def __init__(self):
        self.categories = {1: {"id": 1, "name": "默认", "status": 1}}
        self.words = {}
        self.version = 1

    async def list_categories(self, scope):
        return list(self.categories.values())

    async def list_words(self, scope, **filters):
        records = list(self.words.values())
        for key in ("category_id", "status"):
            if filters[key] is not None:
                records = [item for item in records if item[key] == filters[key]]
        if filters["word"]:
            records = [item for item in records if filters["word"] in item["word"]]
        page, size = filters["page"], filters["page_size"]
        return {"records": records[(page - 1) * size:page * size], "total": len(records), "page": page, "page_size": size}

    def mutation(self, item):
        self.version += 1
        return {"item": item, "version": self.version}

    async def create_category(self, scope, values):
        if any(item["name"] == values["name"] for item in self.categories.values()):
            raise SensitiveWordError("duplicate", status_code=409)
        item = {"id": max(self.categories, default=0) + 1, "status": 1, **values}
        self.categories[item["id"]] = item
        return self.mutation(item)

    async def update_category(self, scope, item_id, values):
        if item_id not in self.categories:
            raise SensitiveWordError("not found", status_code=404)
        self.categories[item_id].update(values)
        return self.mutation(self.categories[item_id])

    async def delete_category(self, scope, item_id):
        if item_id not in self.categories:
            raise SensitiveWordError("not found", status_code=404)
        return self.mutation(self.categories.pop(item_id))

    async def create_word(self, scope, values):
        item = {"id": max(self.words, default=0) + 1, "status": 1, **values}
        self.words[item["id"]] = item
        return self.mutation(item)

    async def update_word(self, scope, item_id, values):
        if item_id not in self.words:
            raise SensitiveWordError("not found", status_code=404)
        self.words[item_id].update(values)
        return self.mutation(self.words[item_id])

    async def delete_word(self, scope, item_id):
        if item_id not in self.words:
            raise SensitiveWordError("not found", status_code=404)
        return self.mutation(self.words.pop(item_id))

    async def import_words(self, scope, category_id, words, *, dry_run=True):
        if category_id not in self.categories:
            raise SensitiveWordError("not found", status_code=404)
        if not dry_run:
            for word in words:
                await self.create_word(scope, {"category_id": category_id, "word": word})
        return {"created_words": len(words), "skipped_words": 0, "dry_run": dry_run, "version": self.version}


class MemoryCache:
    def __init__(self, repository):
        self.repository = repository
        self.version = 0
        self.fail = False

    async def refresh(self, scope):
        if self.fail:
            raise OSError("redis unavailable")
        self.version = self.repository.version
        return {"scope": scope, "version": self.version, "words": tuple(self.repository.words.values())}


class SensitiveWordAdminApiTest(unittest.TestCase):
    def setUp(self):
        self.repository = MemoryRepository()
        self.cache = MemoryCache(self.repository)
        self.runtime = SimpleNamespace(repository=self.repository, cache=self.cache, check=self.check)
        self.service = SensitiveWordAdminService(self.runtime)
        self.settings = SimpleNamespace(sensitive_words=SimpleNamespace(admin_token=SecretStr("admin-secret")))
        app = FastAPI()
        app.add_middleware(InternalRequestAuthMiddleware, header_name="X-DH-Internal-Token", token="gateway-secret")
        app.include_router(router)
        app.dependency_overrides[get_settings] = lambda: self.settings
        app.dependency_overrides[get_admin_service] = lambda: self.service

        async def errors(_request, error):
            code = error.status_code
            message = getattr(error, "message", str(getattr(error, "detail", error)))
            return JSONResponse(status_code=code, content=JsonResult(code=code, message=message, data=getattr(error, "data", None)).model_dump())

        async def validation(_request, error):
            return JSONResponse(status_code=422, content=JsonResult.error(422, "invalid input").model_dump())

        app.add_exception_handler(SensitiveWordError, errors)
        app.add_exception_handler(HTTPException, errors)
        app.add_exception_handler(RequestValidationError, validation)
        self.client = TestClient(app)
        self.client.headers.update({"X-DH-Internal-Token": "gateway-secret", "X-DH-Admin-Token": "admin-secret"})
        self.base = "/internal/sensitive/agent"

    async def check(self, text, scope="agent"):
        return {"action": "allow", "reason": "none", "rule_id": None, "hit_words": [], "version": self.cache.version, "reply": ""}

    def test_requires_both_gateway_and_independent_admin_token(self):
        for header in ("X-DH-Internal-Token", "X-DH-Admin-Token"):
            response = self.client.get(self.base + "/categories", headers={header: "incorrect"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["code"], 403)

    def test_empty_admin_token_disables_management(self):
        self.settings.sensitive_words.admin_token = SecretStr("")
        response = self.client.get(self.base + "/categories")
        self.assertEqual(response.status_code, 503)

    def test_csv_backend_is_unavailable_only_after_authentication(self):
        self.runtime.repository = None
        self.runtime.cache = None
        response = self.client.get(self.base + "/categories", headers={"X-DH-Admin-Token": "bad"})
        self.assertEqual(response.status_code, 403)
        for path in ("/categories", "/words"):
            self.assertEqual(self.client.get(self.base + path).status_code, 503)
        self.assertEqual(self.client.post(self.base + "/check", json={"text": "hello"}).status_code, 503)

    def test_unknown_scope_rejected(self):
        response = self.client.get("/internal/sensitive/public/categories")
        self.assertEqual(response.status_code, 400)

    def test_category_crud_refreshes_committed_changes(self):
        response = self.client.post(self.base + "/categories", json={"name": "  测试  "})
        self.assertEqual(response.status_code, 200)
        item_id = response.json()["data"]["item"]["id"]
        self.assertEqual(self.repository.categories[item_id]["name"], "测试")
        self.assertEqual(self.cache.version, self.repository.version)
        self.assertEqual(self.client.patch(self.base + f"/categories/{item_id}", json={"status": 0}).status_code, 200)
        self.assertEqual(self.repository.categories[item_id]["status"], 0)
        self.assertEqual(self.client.delete(self.base + f"/categories/{item_id}").status_code, 200)
        self.assertNotIn(item_id, self.repository.categories)
        self.assertEqual(self.cache.version, self.repository.version)

    def test_word_crud_filters_and_paginates(self):
        for word in ("one", "two", "three"):
            response = self.client.post(self.base + "/words", json={"category_id": 1, "word": word})
            self.assertEqual(response.status_code, 200)
        self.client.patch(self.base + "/words/2", json={"status": 0})
        response = self.client.get(self.base + "/words?page=2&page_size=1&category_id=1&status=1")
        self.assertEqual(response.json()["data"]["records"][0]["word"], "three")
        self.assertEqual(response.json()["data"]["total"], 2)
        self.assertEqual(self.client.get(self.base + "/words?word=two").json()["data"]["total"], 1)
        self.assertEqual(self.client.delete(self.base + "/words/2").status_code, 200)
        self.assertNotIn(2, self.repository.words)
        self.assertEqual(self.cache.version, self.repository.version)

    def test_patch_rejects_empty_null_unknown_and_wrong_status(self):
        for path in ("categories/1", "words/1"):
            for payload in ({}, {"status": None}, {"status": True}, {"status": "1"}, {"status": 2}, {"unknown": 1}):
                with self.subTest(path=path, payload=payload):
                    self.assertEqual(self.client.patch(self.base + "/" + path, json=payload).status_code, 422)
        self.assertEqual(self.repository.version, 1)

    def test_create_rejects_blank_oversized_and_unknown_fields(self):
        for payload in ({"name": " "}, {"name": "a" * 129}, {"name": "valid", "oops": 1}):
            self.assertEqual(self.client.post(self.base + "/categories", json=payload).status_code, 422)
        for payload in ({"word": " "}, {"word": "a" * 256}, {"word": "valid", "status": True}):
            self.assertEqual(self.client.post(self.base + "/words", json={"category_id": 1, **payload}).status_code, 422)

    def test_pagination_and_identifiers_are_bounded(self):
        for query in ("page=0", "page_size=0", "page_size=201", "status=2", "category_id=0"):
            self.assertEqual(self.client.get(self.base + "/words?" + query).status_code, 422)
        self.assertEqual(self.client.delete(self.base + "/words/0").status_code, 422)

    def test_repository_not_found_and_conflict_are_preserved(self):
        self.assertEqual(self.client.delete(self.base + "/categories/999").status_code, 404)
        self.assertEqual(self.client.post(self.base + "/categories", json={"name": "默认"}).status_code, 409)

    def test_failed_refresh_reports_persisted_mutation(self):
        self.cache.fail = True
        response = self.client.post(self.base + "/words", json={"word": "persisted", "category_id": 1})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["data"], {"persisted": True, "refreshed": False, "version": 2})
        self.assertEqual(self.repository.words[1]["word"], "persisted")

    def test_import_defaults_to_preview_and_normalizes_duplicates(self):
        payload = {"category_id": 1, "csv_text": "word\n  Hello  \nＨＥＬＬＯ\nhello\nworld\n"}
        response = self.client.post(self.base + "/words/import", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["created_words"], 2)
        self.assertEqual(response.json()["data"]["skipped_words"], 2)
        self.assertEqual(self.repository.words, {})
        self.assertEqual(self.cache.version, 0)
        response = self.client.post(self.base + "/words/import", json={**payload, "dry_run": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["word"] for item in self.repository.words.values()], ["Hello", "world"])
        self.assertEqual(self.cache.version, self.repository.version)

    def test_invalid_csv_returns_line_stats_without_writes(self):
        response = self.client.post(self.base + "/words/import", json={"category_id": 1, "csv_text": "word\nvalid\n\"\"\n", "dry_run": False})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["data"]["invalid_lines"][0]["line"], 3)
        self.assertEqual(self.repository.words, {})
        self.assertEqual(self.repository.version, 1)

    def test_import_requires_unambiguous_word_column(self):
        for csv_text in ("name\nword", "word,word\none,two", "word\n\"unterminated", "word\none,extra"):
            with self.subTest(csv_text=csv_text):
                response = self.client.post(self.base + "/words/import", json={"category_id": 1, "csv_text": csv_text})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.repository.words, {})

    def test_import_accepts_valid_word_with_nfkc_expansion(self):
        response = self.client.post(self.base + "/words/import", json={
            "category_id": 1, "csv_text": "word\n" + "㍿" * 100,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["created_words"], 1)

    def test_manual_refresh_returns_only_version_and_count(self):
        self.client.post(self.base + "/words", json={"word": "secret", "category_id": 1})
        response = self.client.post(self.base + "/refresh")
        self.assertEqual(response.json()["data"], {"version": 2, "count": 1})
        self.assertNotIn("secret", response.text)

    def test_check_returns_runtime_decision(self):
        response = self.client.post(self.base + "/check", json={"text": "hello"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["action"], "allow")
        self.assertEqual(response.json()["data"]["hit_words"], [])
