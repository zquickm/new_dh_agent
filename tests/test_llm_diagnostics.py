import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from agents.llm_client import HelloAgentsLLM
from agents.llm_diagnostics import (
    DiagnosticChatOpenAI, StreamDiagnostics, observation_log_summary,
)


class DiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    async def replay(
        self,
        delta,
        finish_reason,
        run_id="run-test",
        enabled=True,
        usage_callback=None,
        usage=None,
    ):
        chunks = [
            {"id": "test", "object": "chat.completion.chunk", "model": "test",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "test", "object": "chat.completion.chunk", "model": "test",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
        ]
        if usage is not None:
            chunks.append(
                {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "model": "test",
                    "choices": [],
                    "usage": usage,
                }
            )
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"

        async def respond(request):
            await asyncio.sleep(0)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
            client = DiagnosticChatOpenAI(
                model="test", api_key="never-log-this-key", base_url="http://test/v1",
                http_async_client=http_client,
            )
            with patch("agents.llm_client.ChatOpenAI", return_value=client):
                llm = HelloAgentsLLM()
            llm.diagnostics_enabled = enabled
            return [item async for item in llm.stream(
                [{"role": "user", "content": "test"}],
                [{"type": "function", "function": {"name": "lookup",
                  "parameters": {"type": "object", "properties": {}}}}],
                run_id=run_id, round_number=2,
                usage_callback=usage_callback,
            )]

    async def test_upstream_usage_is_structured_for_run_persistence(self):
        captured = {}
        await self.replay(
            {"content": "回答"},
            "stop",
            enabled=False,
            usage_callback=captured.update,
            usage={"prompt_tokens": 123, "completion_tokens": 7, "total_tokens": 130},
        )
        self.assertEqual(
            captured,
            {"input_tokens": 123, "output_tokens": 7, "total_tokens": 130},
        )

    async def test_sdk_langchain_and_application_layers_with_valid_tool_call(self):
        delta = {"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                                 "function": {"name": "lookup", "arguments": '{"value":1}'}}]}
        with self.assertLogs("agents.llm_diagnostics", level="INFO") as captured:
            result = await self.replay(delta, "tool_calls")
        self.assertEqual(result[-1][1][0]["args"], {"value": 1})
        logs = "\n".join(captured.output)
        for text in ("stage=upstream_tools", "stage=langchain_tools", "stage=application_tools",
                     "run_id=run-test round=2", "valid_calls=1", "'tool_calls': 1"):
            self.assertIn(text, logs)
        self.assertNotIn("never-log-this-key", logs)

    async def test_invalid_tool_and_text_only_length_finish_are_distinguishable(self):
        delta = {"tool_calls": [{"index": 0, "id": "bad", "type": "function",
                                 "function": {"name": "lookup", "arguments": 'not-json'}}]}
        with self.assertLogs("agents.llm_diagnostics", level="INFO") as captured:
            result = await self.replay(delta, "tool_calls")
        self.assertEqual(result, [])
        logs = "\n".join(captured.output)
        self.assertIn("not-json", logs)
        self.assertIn("valid_calls=0 invalid_calls=1", logs)
        with self.assertLogs("agents.llm_diagnostics", level="INFO") as captured:
            result = await self.replay({"content": "现在我来查询"}, "length")
        self.assertEqual(result, [("现在我来查询", [])])
        self.assertIn("'length': 1", "\n".join(captured.output))
        self.assertIn("'upstream_tool_deltas': 0", "\n".join(captured.output))

    async def test_disabled_diagnostics_and_concurrent_request_isolation(self):
        with patch("agents.llm_diagnostics.logger.info") as log:
            await self.replay({"content": "回答"}, "stop", enabled=False)
            log.assert_not_called()
        with self.assertLogs("agents.llm_diagnostics", level="INFO") as captured:
            await asyncio.gather(
                self.replay({"content": "ONLY_A"}, "stop", "request-A"),
                self.replay({"content": "ONLY_B"}, "stop", "request-B"),
            )
        for line in captured.output:
            if "ONLY_A" in line:
                self.assertIn("run_id=request-A", line)
            if "ONLY_B" in line:
                self.assertIn("run_id=request-B", line)

    def test_bounded_text_does_not_hide_later_tools_or_finish_reason(self):
        diagnostic = StreamDiagnostics(max_chars=64, max_events=1)
        with self.assertLogs("agents.llm_diagnostics", level="INFO") as captured:
            for _ in range(10):
                diagnostic.upstream({"choices": [{"delta": {"content": "x" * 1000}}]})
            diagnostic.upstream({"choices": [{"delta": {"tool_calls": [{"id": "late"}]},
                                              "finish_reason": "length"}]})
            diagnostic.finish(None, "completed")
        logs = "\n".join(captured.output)
        self.assertIn("late", logs)
        self.assertIn("'upstream_tool_deltas': 1", logs)
        self.assertIn("'length': 1", logs)
        self.assertIn("'upstream_text_omitted': 9", logs)
        self.assertLess(len(logs), 2000)


    def test_tool_summaries_exclude_result_bodies(self):
        rag = observation_log_summary(json.dumps({"query": "问题", "chunks": [
            {"source": "报告.pdf", "text": "FULL_DOCUMENT_BODY"}]}))
        database = observation_log_summary(json.dumps({"success": True, "sql": "SELECT value FROM t",
                                                       "rows": [{"value": "FULL_ROW_DATA"}]}))
        self.assertEqual(rag["articles"], ["报告.pdf"])
        self.assertEqual(database["row_count"], 1)
        self.assertEqual(database["sql"], "SELECT value FROM t")
        self.assertNotIn("FULL_DOCUMENT_BODY", str(rag))
        self.assertNotIn("FULL_ROW_DATA", str(database))
