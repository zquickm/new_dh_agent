import json
import unittest
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.duanmian_analysis import (
    ANALYSIS_TYPES, CHEMISTRY_ELEMENTS, HYDROMET_ELEMENTS, execute_duanmian_analysis,
)
from agents.tools.registry import build_tool_executor
from tests.test_agent_loop import FakeLlm


class DuanmianAnalysisTest(unittest.IsolatedAsyncioTestCase):
    def test_all_analysis_types_work_without_optional_parameters(self):
        for analysis in ANALYSIS_TYPES:
            with self.subTest(analysis=analysis):
                output = execute_duanmian_analysis(analysis)
                expected = {"analysis": analysis}
                if analysis == "temp-section":
                    expected["elementType"] = "chemistry"
                self.assertEqual(output.client_events, ({
                    "action_type": "dm_duanmian_analysis", "payload": expected,
                },))
                self.assertTrue(output.content["success"])
                self.assertNotIn("voyage", output.content["parameters"])

    def test_fixed_analysis_rules_and_valid_dates(self):
        output = execute_duanmian_analysis(
            "temp-section", "E1", "2025-11", "hydromet", " 温度 T ",
        )
        self.assertEqual(output.client_events[0]["payload"], {
            "analysis": "temp-section", "section": "E1", "voyage": "2025-11",
            "elementType": "chemistry", "element": "温度 T",
        })
        output = execute_duanmian_analysis("ts-scatter", "E2", "2024-02")
        self.assertEqual(output.client_events[0]["payload"], {
            "analysis": "ts-scatter", "voyage": "2024-02",
        })
        output = execute_duanmian_analysis(
            "surface", elementType="hydromet", element="气温",
        )
        self.assertEqual(output.client_events[0]["payload"]["element"], "气温")
        self.assertEqual(output.client_events[0]["payload"]["elementType"], "hydromet")

    async def test_invalid_arguments_are_rejected_by_registered_tool(self):
        executor = build_tool_executor()
        self.addAsyncCleanup(executor.close)
        cases = [
            {}, {"analysis": "unknown"}, {"analysis": None}, {"analysis": []},
            {"analysis": "depth-curve"},
            {"analysis": "surface", "section": "E4"},
            {"analysis": "surface", "elementType": "unknown"},
            {"analysis": "surface", "element": " "},
            {"analysis": "surface", "element": "temperature_c"},
            {"analysis": "surface", "element": "温度"},
            {"analysis": "surface", "section": 1},
            {"analysis": "surface", "unexpected": True},
        ]
        cases.extend(
            {"analysis": "surface", "voyage": value}
            for value in ("2025-13", "2024-02-29", "2025-02-29", "2025-11-31", "0000-01", "2025-1", "", 2025)
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises((ValueError, TypeError)):
                    await executor.execute_with_events("dm_duanmian_analysis", arguments)

    def test_supported_element_names_are_preserved_in_action(self):
        for element_type, elements in (
            ("chemistry", CHEMISTRY_ELEMENTS), ("hydromet", HYDROMET_ELEMENTS),
        ):
            for element in elements:
                with self.subTest(element=element):
                    output = execute_duanmian_analysis(
                        "surface", elementType=element_type, element=element,
                    )
                    self.assertEqual(output.client_events[0]["payload"]["element"], element)

    async def test_agent_emits_action_and_returns_receipt_to_model(self):
        llm = FakeLlm([
            ([], [{"id": "call-analysis", "name": "dm_duanmian_analysis", "args": {
                "analysis": "temp-section", "section": "E1", "voyage": "2025-11",
                "element": "温度 T",
            }}]),
            (["已发送断面专题分析出图指令。"], []),
        ])
        agent = AgentLoop(llm, build_tool_executor())
        self.addAsyncCleanup(agent.close)
        with patch("services.context_service.expand_query_synonyms", side_effect=lambda value: value):
            events = [event async for event in agent.run_stream("画 E1 断面温度剖面图", [])]
        actions = [event for event in events if event.type == "map_action"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].tool_call_id, "call-analysis")
        self.assertEqual(actions[0].data, {
            "seq": 1, "action_type": "dm_duanmian_analysis", "payload": {
                "analysis": "temp-section", "section": "E1", "voyage": "2025-11",
                "element": "温度 T", "elementType": "chemistry",
            },
        })
        receipt = json.loads(llm.calls[1][0][-1]["content"])
        self.assertEqual(receipt["message"], "action received")

    async def test_invalid_analysis_emits_no_action_and_gives_model_correction_detail(self):
        llm = FakeLlm([
            ([], [{"id": "call-invalid", "name": "dm_duanmian_analysis", "args": {
                "analysis": "invalid",
            }}]),
            (["请明确需要的图表类型。"], []),
        ])
        agent = AgentLoop(llm, build_tool_executor())
        self.addAsyncCleanup(agent.close)
        with patch("services.context_service.expand_query_synonyms", side_effect=lambda value: value):
            events = [event async for event in agent.run_stream("断面分析", [])]
        self.assertFalse(any(event.type == "map_action" for event in events))
        finished = next(event for event in events if event.type == "tool_finished")
        self.assertFalse(finished.success)
        self.assertIn("analysis", llm.calls[1][0][-1]["content"])
