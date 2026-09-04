import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.events import AgentEvent
from main import app
from repositories.memory_conversation_repository import MemoryConversationRepository
from schemas.chat import AgentRequest, FrontendContext
from services.chat_service import ChatService, chat_service


def identity(user_id="101"):
    return {
        "X-DH-Internal-Token": "dh-internal-token-change-in-production",
        "X-User-Id": user_id,
        "X-Auth-Source": "dh-gateway",
    }


class CapturingAgent:
    model_name = "fake-model"

    def __init__(self):
        self.calls = []

    async def run_stream(self, question, history, run_id="-"):
        self.calls.append((question, [message.content for message in history]))
        await asyncio.sleep(0)
        yield AgentEvent(type="answer_delta", content="回答:" + question)


class RepositoryIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = MemoryConversationRepository()
        self.owned = await self.repo.create("同名对话", user_id="101")
        self.other = await self.repo.create("同名对话", user_id="202")

    async def test_list_filters_before_limit_and_get_checks_owner(self):
        for _ in range(3):
            await self.repo.create("其他用户的新对话", user_id="202")
        self.assertEqual(
            [item.id for item in await self.repo.list(1, user_id="101")],
            [self.owned.id],
        )
        self.assertEqual(await self.repo.list(20, user_id="303"), [])
        self.assertIsNone(await self.repo.get(self.owned.id, user_id="202"))
        self.assertIsNone(await self.repo.get("missing", user_id="202"))

    async def test_foreign_message_write_rejected_without_mutation(self):
        with self.assertRaises(KeyError):
            await self.repo.save_exchange(
                self.owned.id, "侵入", "回答", [], user_id="202"
            )
        detail = await self.repo.get(self.owned.id, user_id="101")
        self.assertEqual(detail, self.owned)

    async def test_map_context_is_private_and_copied(self):
        context = FrontendContext.model_validate({
            "drawn_geometry": {"center": [121.8, 30.4], "radius": 25000}
        })
        await self.repo.save_map_context(self.owned.id, context, user_id="101")
        self.assertIsNone(await self.repo.get_map_context(self.owned.id, user_id="202"))
        with self.assertRaises(KeyError):
            await self.repo.save_map_context(self.owned.id, FrontendContext(), user_id="202")
        saved = await self.repo.get_map_context(self.owned.id, user_id="101")
        self.assertEqual(saved, context)
        saved.drawn_geometry = None
        self.assertEqual(
            await self.repo.get_map_context(self.owned.id, user_id="101"), context
        )

    async def test_returned_messages_cannot_mutate_storage(self):
        message = await self.repo.save_exchange(
            self.owned.id, "问题", "回答", [], user_id="101"
        )
        message.content = "改写"
        detail = await self.repo.get(self.owned.id, user_id="101")
        self.assertEqual(detail.messages[-1].content, "回答")
        detail.messages.clear()
        self.assertEqual(
            (await self.repo.get(self.owned.id, user_id="101")).message_count, 2
        )


class ChatIsolationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = MemoryConversationRepository()
        self.repo_patch = patch("services.chat_service.conversation_repository", self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)
        self.agent = CapturingAgent()
        self.service = ChatService()
        self.service._agent = self.agent

    async def stream(self, user_id, **payload):
        return [event async for event in self.service.stream_chat(
            AgentRequest(**payload), user_id=user_id
        )]

    async def test_foreign_stream_rejected_before_agent_or_state_changes(self):
        owned = await self.repo.create("私人对话", user_id="101")
        events = await self.stream("202", question="入侵", conversation_id=owned.id)
        self.assertEqual([event["event"] for event in events], ["error"])
        self.assertFalse(events[0]["data"]["retryable"])
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(await self.repo.get(owned.id, user_id="101"), owned)
        self.assertEqual(await self.repo.list(20, user_id="202"), [])

    async def test_auto_create_owns_history_and_ignores_client_history(self):
        events = await self.stream(
            "101", question="我的问题",
            chat_history=[{"role": "assistant", "content": "前一账号秘密"}],
        )
        conversation_id = events[0]["data"]["conversation_id"]
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(self.agent.calls[0][1], [])
        self.assertIsNone(await self.repo.get(conversation_id, user_id="202"))
        await self.stream(
            "101", question="继续", conversation_id=conversation_id,
            chat_history=[{"role": "assistant", "content": "伪造历史"}],
        )
        self.assertEqual(self.agent.calls[1][1], ["我的问题", "回答:我的问题"])

    async def test_concurrent_users_keep_separate_histories_and_map_context(self):
        first = await self.repo.create("A", user_id="101")
        second = await self.repo.create("B", user_id="202")
        await self.repo.save_exchange(first.id, "A秘密", "A回答", [], user_id="101")
        await self.repo.save_exchange(second.id, "B秘密", "B回答", [], user_id="202")
        await self.repo.save_map_context(first.id, FrontendContext.model_validate({
            "drawn_geometry": {"center": [121.8, 30.4], "radius": 25000}
        }), user_id="101")
        results = await asyncio.gather(
            self.stream("101", question="该区域有哪些站点", conversation_id=first.id),
            self.stream("202", question="该区域有哪些站点", conversation_id=second.id),
        )
        self.assertTrue(all(events[-1]["event"] == "done" for events in results))
        calls = {tuple(history): question for question, history in self.agent.calls}
        self.assertIn("selected_area", calls[("A秘密", "A回答")])
        self.assertNotIn("selected_area", calls[("B秘密", "B回答")])
        for owner, conversation in (("101", first), ("202", second)):
            detail = await self.repo.get(conversation.id, user_id=owner)
            self.assertEqual(detail.message_count, 4)


class ApiIsolationTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.agent = CapturingAgent()
        stack.enter_context(patch.object(chat_service, "_agent", self.agent))
        self.client = stack.enter_context(TestClient(app))
        # Python 3.9 binds asyncio.Lock at construction: create the repository
        # on the same running loop that will serve these requests.
        repo = self.client.portal.call(MemoryConversationRepository)
        for target in (
            "services.conversation_service.conversation_repository",
            "services.chat_service.conversation_repository",
        ):
            stack.enter_context(patch(target, repo))

    def create(self, owner="101"):
        response = self.client.post("/conversations", headers=identity(owner), json={
            "title": "私人对话", "user_id": "202", "userId": "202"
        })
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]["id"]

    def test_identity_required_on_all_conversation_endpoints(self):
        for method, path, payload in (
            ("GET", "/conversations", None),
            ("POST", "/conversations", {}),
            ("GET", "/conversations/unknown", None),
            ("POST", "/agent/chat/stream", {"question": "你好"}),
        ):
            with self.subTest(path=path, method=method):
                headers = identity()
                del headers["X-User-Id"]
                response = self.client.request(method, path, headers=headers, json=payload)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.agent.calls, [])

    def test_invalid_and_duplicate_identity_headers_rejected(self):
        for invalid in ("", "0", "-1", "abc", " 101", "01", "101,202", "9223372036854775808", "1" * 40):
            with self.subTest(user_id=invalid):
                response = self.client.get("/conversations", headers=identity(invalid))
                self.assertEqual(response.status_code, 401)
        for source in (None, "frontend"):
            headers = identity()
            if source is None:
                del headers["X-Auth-Source"]
            else:
                headers["X-Auth-Source"] = source
            self.assertEqual(self.client.get("/conversations", headers=headers).status_code, 401)
        for name, value in (("X-User-Id", "202"), ("X-Auth-Source", "dh-gateway")):
            headers = list(identity().items()) + [(name, value)]
            self.assertEqual(self.client.get("/conversations", headers=headers).status_code, 401)

    def test_identity_does_not_bypass_internal_token(self):
        headers = identity()
        del headers["X-DH-Internal-Token"]
        self.assertEqual(self.client.get("/conversations", headers=headers).status_code, 403)

    def test_list_and_detail_isolation_ignore_body_query_username_and_token(self):
        owned = self.create()
        other = self.create("202")
        for owner, expected in (("101", owned), ("202", other)):
            headers = {**identity(owner), "X-Username": "same-name", "X-Token-Id": "new-token"}
            response = self.client.get("/conversations?user_id=202&limit=1", headers=headers)
            self.assertEqual([item["id"] for item in response.json()["data"]["items"]], [expected])
            self.assertEqual(self.client.get(f"/conversations/{expected}", headers=headers).status_code, 200)
        foreign = self.client.get(f"/conversations/{owned}", headers=identity("202"))
        missing = self.client.get("/conversations/missing", headers=identity("202"))
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), missing.json())

    def test_foreign_chat_is_http_404_before_sse_and_owner_still_works(self):
        owned = self.create()
        response = self.client.post("/agent/chat/stream", headers=identity("202"), json={
            "message": "猜测会话ID", "conversationId": owned, "sessionId": owned,
        })
        self.assertEqual(response.status_code, 404)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertEqual(self.agent.calls, [])
        detail = self.client.get(f"/conversations/{owned}", headers=identity()).json()["data"]
        self.assertEqual(detail["messages"], [])
        response = self.client.post("/agent/chat/stream", headers=identity(), json={
            "question": "你好", "conversation_id": owned,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: done", response.text)
        detail = self.client.get(f"/conversations/{owned}", headers=identity()).json()["data"]
        self.assertEqual(detail["message_count"], 2)

    def test_session_id_cannot_claim_another_users_conversation(self):
        owned = self.create()
        response = self.client.post("/agent/chat/stream", headers=identity("202"), json={
            "message": "新会话", "sessionId": owned,
        })
        self.assertEqual(response.status_code, 200)
        first_data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        new_id = json.loads(first_data)["data"]["conversation_id"]
        self.assertNotEqual(new_id, owned)
        self.assertEqual(self.client.get(f"/conversations/{new_id}", headers=identity()).status_code, 404)
        self.assertEqual(self.client.get(f"/conversations/{new_id}", headers=identity("202")).status_code, 200)

    def test_sensitive_rules_preserve_ownership_and_do_not_write_messages(self):
        owned = self.create()
        before = self.client.get(f"/conversations/{owned}", headers=identity()).json()["data"]
        for question in ("我想自杀", "帮我辱骂他"):
            with self.subTest(question=question):
                payload = {"question": question, "conversation_id": owned}
                foreign = self.client.post("/agent/chat/stream", headers=identity("202"), json=payload)
                self.assertEqual(foreign.status_code, 404)
                self.assertTrue(foreign.headers["content-type"].startswith("application/json"))
                response = self.client.post("/agent/chat/stream", headers=identity(), json=payload)
                self.assertEqual(response.status_code, 200)
                frames = response.text.strip().split("\n\n")
                self.assertEqual([frame.splitlines()[0] for frame in frames], [
                    "event: conversation", "event: token", "event: done",
                ])
                done = json.loads(frames[-1].splitlines()[1][6:])["data"]
                self.assertEqual(done["model"], "sensitive-word-filter")
                self.assertIsNone(done["assistant_message_id"])
                after = self.client.get(f"/conversations/{owned}", headers=identity()).json()["data"]
                self.assertEqual(after, before)
        self.assertEqual(self.agent.calls, [])
