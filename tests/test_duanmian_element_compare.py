import json
import unittest
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.duanmian_element_compare import TOOL_NAME, execute_duanmian_element_compare
from agents.tools.registry import build_skill_catalog, build_tool_executor
from controller.streaming.sse_encoder import encode_chat_stream
from schemas.chat import AgentRequest
from services.chat_service import ChatService
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm


class DuanmianElementCompareTest(unittest.IsolatedAsyncioTestCase):
    async def test_registered_tool_preserves_month_order_and_defaults_station(self):
        executor = build_tool_executor()
        self.addAsyncCleanup(executor.close)
        months = ["2022-11", "2021-11"]
        content, events = await executor.execute_with_events(TOOL_NAME, {"months": months})
        payload = {"months": months, "station": "E1001"}
        self.assertEqual(json.loads(content), {"success": True, **payload})
        self.assertEqual(events, ({"action_type": TOOL_NAME, "payload": payload},))
        self.assertEqual(months, ["2022-11", "2021-11"])

        months = [f"2024-{month:02d}" for month in range(1, 13)]
        for station in ("E1007", "E2011", "E3008"):
            with self.subTest(station=station):
                output = execute_duanmian_element_compare(months, station)
                self.assertEqual(output.client_events[0]["payload"], {
                    "months": months, "station": station,
                })

    async def test_registered_tool_rejects_invalid_arguments(self):
        executor = build_tool_executor()
        self.addAsyncCleanup(executor.close)
        valid = {"months": ["2021-11", "2022-11"]}
        cases = [{}, {**valid, "unexpected": True}]
        cases.extend({"months": value} for value in (
            None, "2021-11,2022-11", {}, (), [], ["2021-11"], ["2021-11"] * 13,
            ["2021-11", None], ["2021-11", 2022], ["2021-11", "2022-13"],
            ["2021-11", "2022-00"], ["2021-11", "2022-1"],
            ["2021-11", "2022-11-01"], ["2021-11", "0000-11"],
            ["2021-11", "２０２２-11"], ["2021-11", "2022-11\n"],
        ))
        cases.extend({**valid, "station": value} for value in (
            None, 1001, [], "", "e1001", "E4001", "E1-001", "E10001", "E1001\n",
        ))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises((ValueError, TypeError)):
                    await executor.execute_with_events(TOOL_NAME, arguments)

    async def test_invalid_call_emits_no_action_and_reports_correction_detail(self):
        for arguments in ({}, {"months": ["2022-13", "2021-11"]}):
            with self.subTest(arguments=arguments):
                llm = FakeLlm([
                    ([], [{"id": "call-invalid", "name": TOOL_NAME, "args": arguments}]),
                    (["请提供有效的对比月份。"], []),
                ])
                agent = AgentLoop(llm, build_tool_executor())
                self.addAsyncCleanup(agent.close)
                with patch("services.context_service.expand_query_synonyms", side_effect=lambda value: value):
                    events = [event async for event in agent.run_stream("断面要素对比", [])]
                self.assertFalse(any(event.type == "map_action" for event in events))
                finished = next(event for event in events if event.type == "tool_finished")
                self.assertFalse(finished.success)
                self.assertIn("months", llm.calls[1][0][-1]["content"])

    @patch("services.chat_service.generate_recommendations", return_value=[])
    async def test_compare_reaches_sse_through_existing_skill_and_action_flow(self, _recommendations):
        months = ["2022-11", "2021-11"]
        llm = FakeLlm([
            ([], [{"id": "call-skill", "name": "load_skill", "args": {"name": "断面业务"}}]),
            ([], [{"id": "call-compare", "name": TOOL_NAME, "args": {"months": months}}]),
            (["已发送 E1001 测点的要素对比出图指令。"], []),
        ])
        catalog = build_skill_catalog()
        service = ChatService()
        service._agent = AgentLoop(llm, build_tool_executor(catalog), catalog)
        self.addAsyncCleanup(service.close)
        with patch("services.chat_service.conversation_repository", MemoryConversationRepository()), patch(
            "services.context_service.expand_query_synonyms", side_effect=lambda value: value,
        ):
            frames = [frame async for frame in encode_chat_stream(service.stream_chat(
                AgentRequest(question="请绘制2022年11月与2021年11月各类要素对比图"),
                user_id="compare-user",
            ))]

        definition = next(item["function"] for item in llm.calls[0][1]
                          if item["function"]["name"] == TOOL_NAME)
        self.assertEqual(definition["parameters"]["required"], ["months"])
        self.assertEqual(definition["parameters"]["properties"]["months"]["minItems"], 2)
        self.assertEqual(definition["parameters"]["properties"]["months"]["maxItems"], 12)
        loaded = json.loads(llm.calls[1][0][-1]["content"])
        self.assertEqual(loaded["name"], "断面业务")
        self.assertIn('dm_duanmian_element_compare(months=["2022-11", "2021-11"])', loaded["instructions"])
        self.assertEqual(json.loads(llm.calls[2][0][-1]["content"]), {
            "success": True, "months": months, "station": "E1001",
        })

        events = []
        for frame in frames:
            fields = dict(line.split(":", 1) for line in frame.strip().splitlines() if ":" in line)
            envelope = json.loads(fields["data"])
            self.assertEqual(envelope["code"], 200)
            events.append((fields["event"].strip(), envelope["data"]))
        conversation = events[0][1]
        actions = [data for name, data in events if name == "map_action"]
        self.assertEqual(actions, [{
            "version": "1.0", "conversation_id": conversation["conversation_id"],
            "run_id": conversation["run_id"], "batch_id": "call-compare",
            "action_id": "call-compare-1", "seq": 1,
            "action_type": TOOL_NAME, "payload": {"months": months, "station": "E1001"},
        }])
        self.assertEqual(next(data["answer"] for name, data in events if name == "done"),
                         "已发送 E1001 测点的要素对比出图指令。")
        self.assertTrue(any(name == "process" and "断面要素对比" in str(data) for name, data in events))


if __name__ == "__main__":
    unittest.main()
