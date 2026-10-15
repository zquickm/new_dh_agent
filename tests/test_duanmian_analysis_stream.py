import json
import unittest
from unittest.mock import patch

from agents.agent_loop import AgentLoop
from agents.tools.registry import build_skill_catalog, build_tool_executor
from controller.streaming.sse_encoder import encode_chat_stream
from schemas.chat import AgentRequest
from services.chat_service import ChatService
from tests.fakes.memory_conversation_repository import MemoryConversationRepository
from tests.test_agent_loop import FakeLlm


class DuanmianAnalysisStreamTest(unittest.IsolatedAsyncioTestCase):
    @patch("services.chat_service.generate_recommendations", return_value=[])
    async def test_registered_tool_reaches_sse_with_metadata_and_chinese_display_name(self, _recommendations):
        llm = FakeLlm([
            ([], [{"id": "call-skill", "name": "load_skill", "args": {"name": "断面业务"}}]),
            ([], [{"id": "call-analysis", "name": "dm_duanmian_analysis", "args": {
                "analysis": "temp-section", "section": "E1", "voyage": "2022-05",
                "element": "温度 T",
            }}]),
            (["已发送出图指令。"], []),
        ])
        service = ChatService()
        catalog = build_skill_catalog()
        service._agent = AgentLoop(llm, build_tool_executor(catalog), catalog)
        self.addAsyncCleanup(service.close)
        repository = MemoryConversationRepository()
        with patch("services.chat_service.conversation_repository", repository), patch(
            "services.context_service.expand_query_synonyms", side_effect=lambda value: value,
        ):
            frames = [frame async for frame in encode_chat_stream(service.stream_chat(
                AgentRequest(question="2022年5月 E1 断面的温度情况"), user_id="analysis-user",
            ))]
        tool = next(item["function"] for item in llm.calls[0][1]
                    if item["function"]["name"] == "dm_duanmian_analysis")
        self.assertIn("调用前先加载“断面业务” Skill", tool["description"])
        self.assertNotIn("depth-curve", tool["parameters"]["properties"]["analysis"]["enum"])
        self.assertIn("已明确要素时必须传 element", tool["description"])
        self.assertIn("已明确要素时必须填写", tool["parameters"]["properties"]["element"]["description"])
        loaded = json.loads(llm.calls[1][0][-1]["content"])
        self.assertEqual(loaded["name"], "断面业务")
        self.assertIn("temp-section", loaded["instructions"])
        self.assertIn('voyage="2022-05", element="温度 T"', loaded["instructions"])
        self.assertNotIn("depth-curve", loaded["instructions"])
        self.assertNotIn("空间联动", loaded["instructions"])
        events = []
        for frame in frames:
            fields = {
                key.strip(): value.strip()
                for line in frame.strip().splitlines()
                if ":" in line
                for key, value in [line.split(":", 1)]
            }
            envelope = json.loads(fields["data"])
            self.assertEqual(envelope["code"], 200)
            events.append((fields["event"], envelope["data"]))
        conversation = events[0][1]
        actions = [data for name, data in events if name == "map_action"]
        self.assertEqual(actions, [{
            "version": "1.0", "conversation_id": conversation["conversation_id"],
            "run_id": conversation["run_id"], "batch_id": "call-analysis",
            "action_id": "call-analysis-1", "seq": 1,
            "action_type": "dm_duanmian_analysis", "payload": {
                "analysis": "temp-section", "section": "E1", "voyage": "2022-05",
                "elementType": "chemistry", "element": "温度 T",
            },
        }])
        self.assertEqual(events[-2][0], "done")
        self.assertEqual(events[-2][1]["answer"], "已发送出图指令。")
        self.assertEqual(ChatService._tool_display_name("dm_duanmian_analysis"), "断面专题分析")
