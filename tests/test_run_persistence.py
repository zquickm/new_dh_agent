import unittest
from datetime import datetime, timedelta, timezone

from services.chat_service import ChatService
from tests.fakes.memory_conversation_repository import MemoryConversationRepository


class RunPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = MemoryConversationRepository()
        self.conversation = await self.repository.create("运行测试", user_id="101")

    async def test_begin_exchange_persists_messages_and_running_record(self):
        user, assistant = await self.repository.begin_exchange(
            self.conversation.id,
            "先写入问题",
            run_id="run-1",
            user_id="101",
            model="fake-model",
            owner_instance_id="instance-1",
        )

        detail = await self.repository.get(self.conversation.id, user_id="101")
        run = await self.repository.get_run(
            self.conversation.id, "run-1", user_id="101"
        )
        self.assertEqual([message.id for message in detail.messages], [user.id, assistant.id])
        self.assertEqual(assistant.status, "streaming")
        self.assertEqual(run.status, "running")
        self.assertEqual(run.user_message_id, user.id)
        self.assertEqual(run.assistant_message_id, assistant.id)

    async def test_final_message_and_run_transition_together(self):
        _, assistant = await self.repository.begin_exchange(
            self.conversation.id,
            "完成问题",
            run_id="run-2",
            user_id="101",
            model="initial-model",
        )

        await self.repository.update_assistant_message(
            assistant.id,
            "最终回答",
            [{"citation_id": 1}],
            status="completed",
            run_status="completed",
            model="actual-model",
            user_id="101",
        )

        run = await self.repository.get_run(
            self.conversation.id, "run-2", user_id="101"
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.model, "actual-model")
        self.assertIsNotNone(run.finished_at)
        self.assertIsNotNone(run.duration_ms)

    async def test_stale_run_recovery_marks_assistant_failed(self):
        _, assistant = await self.repository.begin_exchange(
            self.conversation.id,
            "中断问题",
            run_id="run-3",
            user_id="101",
            model="fake-model",
        )
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)

        recovered = await self.repository.recover_stale_runs(cutoff)

        run = await self.repository.get_run(
            self.conversation.id, "run-3", user_id="101"
        )
        detail = await self.repository.get(self.conversation.id, user_id="101")
        persisted_assistant = next(
            message for message in detail.messages if message.id == assistant.id
        )
        self.assertEqual(recovered, 1)
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(run.error_code, "AGENT_INTERRUPTED")
        self.assertEqual(persisted_assistant.status, "failed")
        self.assertEqual(persisted_assistant.error_code, "AGENT_INTERRUPTED")

    async def test_run_query_is_tenant_isolated(self):
        await self.repository.begin_exchange(
            self.conversation.id,
            "隔离问题",
            run_id="run-4",
            user_id="101",
        )

        self.assertIsNone(
            await self.repository.get_run(
                self.conversation.id, "run-4", user_id="202"
            )
        )

    def test_error_message_is_redacted_and_truncated(self):
        error = RuntimeError(
            "password=secret token:abc api_key=xyz "
            "mysql://agent:db-password@127.0.0.1/db "
            + "x" * 2500
        )

        message = ChatService._safe_error_message(error)

        self.assertNotIn("secret", message)
        self.assertNotIn("abc", message)
        self.assertNotIn("xyz", message)
        self.assertNotIn("db-password", message)
        self.assertLessEqual(len(message), 2000)


if __name__ == "__main__":
    unittest.main()
