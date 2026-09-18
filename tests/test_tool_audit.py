import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.executor import ToolExecutor
from services.tool_audit_service import (
    PersistentToolAuditRecorder,
    sanitize_arguments,
    summarize_result,
)
from services.synonym_service import build_synonym_matcher
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm


class CapturingAuditRecorder:
    def __init__(self, fail_start: bool = False):
        self.fail_start = fail_start
        self.started_calls = []
        self.finished_calls = []

    async def started(self, **kwargs):
        self.started_calls.append(kwargs)
        if self.fail_start:
            raise RuntimeError("audit unavailable")

    async def finished(self, **kwargs):
        self.finished_calls.append(kwargs)


class ToolAuditSanitizerTest(unittest.TestCase):
    def test_arguments_are_redacted(self):
        arguments, truncated = sanitize_arguments(
            {
                "password": "secret",
                "nested": {"authorization": "Bearer abc"},
                "url": "mysql://agent:db-password@127.0.0.1/db",
                "question": "token=xyz query",
            }
        )

        rendered = str(arguments)
        self.assertFalse(truncated)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("Bearer abc", rendered)
        self.assertNotIn("db-password", rendered)
        self.assertNotIn("xyz", rendered)

    def test_database_result_keeps_metadata_but_not_rows(self):
        summary, size, digest, truncated = summarize_result(
            "database_query",
            '{"success": true, "sql": "select secret", '
            '"rows": [{"name": "private"}], "row_count": 1}',
        )

        self.assertIn('"row_count": 1', summary)
        self.assertIn('"columns": ["name"]', summary)
        self.assertNotIn("private", summary)
        self.assertNotIn("select secret", summary)
        self.assertGreater(size, 0)
        self.assertEqual(len(digest), 64)
        self.assertFalse(truncated)

    def test_structural_argument_limit_sets_truncated_flag(self):
        arguments, truncated = sanitize_arguments(
            {f"key-{index}": index for index in range(101)}
        )

        self.assertTrue(truncated)
        self.assertEqual(len(arguments), 100)

    def test_observation_result_does_not_keep_measurement_values(self):
        summary, _, _, _ = summarize_result(
            "observation_service",
            '{"operation": "observation_range", '
            '"records": [{"water_temperature": 19.7}]}',
        )

        self.assertIn('"records": 1', summary)
        self.assertNotIn("19.7", summary)


class ToolAuditAgentLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        synonym_patch = patch(
            "services.synonym_service._active_synonym_matcher",
            build_synonym_matcher(
                [{"standardTerm": "test", "terms": [{"term": "testing"}]}]
            ),
        )
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)

    @staticmethod
    def _tool_call(name="test_tool"):
        return {
            "id": "call-1",
            "name": name,
            "args": {"value": "hello"},
            "type": "tool_call",
        }

    async def test_audit_start_is_fail_closed_before_tool_execution(self):
        executed = 0

        async def tool(value):
            nonlocal executed
            executed += 1
            return value

        tools = ToolExecutor()
        tools.register("test_tool", "test", tool)
        agent = AgentLoop(FakeLlm([([], [self._tool_call()])]), tools)
        recorder = CapturingAuditRecorder(fail_start=True)

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            _ = [
                event
                async for event in agent.run_stream(
                    "test", [], audit_recorder=recorder
                )
            ]
        self.assertEqual(executed, 0)
        self.assertEqual(len(recorder.started_calls), 1)
        self.assertEqual(recorder.finished_calls, [])

    async def test_user_cancelled_tool_is_not_recorded_as_service_interruption(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def tool(value):
            del value
            started.set()
            await release.wait()

        tools = ToolExecutor()
        tools.register("test_tool", "test", tool)
        agent = AgentLoop(FakeLlm([([], [self._tool_call()])]), tools)
        recorder = CapturingAuditRecorder()

        async def collect():
            return [
                event
                async for event in agent.run_stream(
                    "testing", [], audit_recorder=recorder
                )
            ]

        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel("USER_CANCELLED")
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(recorder.finished_calls[0]["error_code"], "USER_CANCELLED")
        self.assertEqual(recorder.finished_calls[0]["error_type"], "UserCancelled")
        self.assertFalse(recorder.finished_calls[0]["retryable"])

    async def test_success_is_finalized_before_client_finish_event(self):
        async def tool(value):
            return {"echo": value}

        tools = ToolExecutor()
        tools.register("test_tool", "test", tool)
        agent = AgentLoop(
            FakeLlm([([], [self._tool_call()]), (["done"], [])]), tools
        )
        recorder = CapturingAuditRecorder()

        events = [
            event
            async for event in agent.run_stream(
                "test", [], audit_recorder=recorder
            )
        ]

        self.assertEqual(recorder.started_calls[0]["sequence_no"], 1)
        self.assertEqual(recorder.finished_calls[0]["status"], "succeeded")
        self.assertEqual(
            [event.type for event in events],
            ["tool_started", "tool_finished", "answer_delta"],
        )

    async def test_tool_failure_is_recorded_without_failing_run(self):
        async def tool(value):
            del value
            raise TimeoutError("upstream timeout")

        tools = ToolExecutor()
        tools.register("test_tool", "test", tool)
        agent = AgentLoop(
            FakeLlm([([], [self._tool_call()]), (["fallback"], [])]), tools
        )
        recorder = CapturingAuditRecorder()

        events = [
            event
            async for event in agent.run_stream(
                "test", [], audit_recorder=recorder
            )
        ]

        finished = recorder.finished_calls[0]
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "TOOL_TIMEOUT")
        self.assertTrue(finished["retryable"])
        self.assertFalse(events[1].success)
        self.assertEqual(events[-1].content, "fallback")

    async def test_semantic_database_failure_is_a_failed_tool_call(self):
        async def tool(value):
            del value
            return {
                "success": False,
                "error": {"type": "QueryError", "message": "bad query"},
            }

        tools = ToolExecutor()
        tools.register("database_query", "test", tool)
        call = self._tool_call("database_query")
        agent = AgentLoop(FakeLlm([([], [call]), (["fallback"], [])]), tools)
        recorder = CapturingAuditRecorder()

        events = [
            event
            async for event in agent.run_stream(
                "test", [], audit_recorder=recorder
            )
        ]

        finished = recorder.finished_calls[0]
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error_code"], "TOOL_DATABASE_ERROR")
        self.assertEqual(finished["error_type"], "QueryError")
        self.assertFalse(events[1].success)


class ToolAuditRepositoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = MemoryConversationRepository()
        self.conversation = await self.repository.create("audit", user_id="101")
        _, self.assistant = await self.repository.begin_exchange(
            self.conversation.id,
            "question",
            run_id="run-audit",
            user_id="101",
        )

    async def test_lifecycle_and_tenant_isolation(self):
        await self.repository.begin_tool_call(
            "run-audit",
            tool_call_id="call-audit",
            round_number=1,
            sequence_no=1,
            tool_name="test_tool",
            arguments={"value": "safe"},
            arguments_truncated=False,
        )
        await self.repository.finish_tool_call(
            "run-audit",
            tool_call_id="call-audit",
            status="succeeded",
            result_summary="ok",
            result_size_bytes=2,
            result_sha256="a" * 64,
            result_truncated=False,
            duration_ms=3,
        )

        items = await self.repository.list_tool_calls(
            self.conversation.id, "run-audit", user_id="101"
        )
        hidden = await self.repository.list_tool_calls(
            self.conversation.id, "run-audit", user_id="202"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].status, "succeeded")
        self.assertEqual(hidden, [])

    async def test_persistent_recorder_uses_tool_specific_safe_summary(self):
        recorder = PersistentToolAuditRecorder(
            "run-audit", repository=self.repository
        )
        await recorder.started(
            tool_call_id="call-database",
            round_number=1,
            sequence_no=1,
            tool_name="database_query",
            arguments={"password": "secret", "question": "query"},
        )
        await recorder.finished(
            tool_call_id="call-database",
            status="succeeded",
            result=(
                '{"success": true, "sql": "select private", '
                '"rows": [{"name": "sensitive-row"}], "row_count": 1}'
            ),
            duration_ms=2,
        )

        item = await self.repository.get_tool_call(
            self.conversation.id,
            "run-audit",
            "call-database",
            user_id="101",
        )
        self.assertEqual(item.arguments["password"], "***")
        self.assertIn('"row_count": 1', item.result_summary)
        self.assertNotIn("sensitive-row", item.result_summary)
        self.assertNotIn("select private", item.result_summary)

    async def test_stale_recovery_interrupts_running_tool_call(self):
        await self.repository.begin_tool_call(
            "run-audit",
            tool_call_id="call-running",
            round_number=1,
            sequence_no=1,
            tool_name="test_tool",
            arguments={},
            arguments_truncated=False,
        )

        await self.repository.recover_stale_runs(
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=1)
        )

        item = await self.repository.get_tool_call(
            self.conversation.id,
            "run-audit",
            "call-running",
            user_id="101",
        )
        self.assertEqual(item.status, "interrupted")
        self.assertEqual(item.error_code, "AGENT_INTERRUPTED")


if __name__ == "__main__":
    unittest.main()
