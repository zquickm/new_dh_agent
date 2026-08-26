import asyncio
import json
import unittest

from agents.agent_loop import AgentLoop
from agents.events import AgentEvent
from agents.tools.executor import ToolExecutor
from controller.streaming.sse_encoder import encode_chat_stream
from repositories.memory_conversation_repository import conversation_repository
from schemas.chat import AgentRequest
from services.chat_service import ChatService


class FakeAgent:
    model_name = "fake-model"

    async def run_stream(self, question, history):
        del question, history
        yield AgentEvent(
            type="tool_started",
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(
            type="retrieval",
            data={
                "tool_call_id": "call-1",
                "tool_name": "rag_search",
                "query": "问题",
                "result_count": 1,
                "chunks": [
                    {
                        "rank": 1,
                        "source": "资料.pdf",
                        "preview": "检索片段",
                        "page_index": 0,
                    }
                ],
            },
            sources=[
                {
                    "citation_id": 1,
                    "source": "资料.pdf",
                    "line": None,
                    "score": 0.9,
                    "preview": "检索片段",
                    "title": "资料",
                    "url": None,
                    "page_index": 0,
                    "chunk_id": "chunk-1",
                    "knowledge_id": "kb-1",
                }
            ],
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(
            type="tool_finished",
            content="检索完成",
            round_number=1,
            tool_name="rag_search",
            tool_call_id="call-1",
        )
        yield AgentEvent(type="answer_delta", content="你好[[cite:1]]", round_number=2)
        yield AgentEvent(type="answer_delta", content="世界", round_number=2)


class ChatServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_frontend_geometry_reaches_agent_but_is_not_persisted_in_question(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.question = ""

            async def run_stream(self, question, history):
                del history
                self.question = question
                yield AgentEvent(
                    type="answer_delta",
                    content="区域内有观测站",
                    round_number=1,
                )

        conversation = await conversation_repository.create("地图选区测试")
        agent = CapturingAgent()
        service = ChatService()
        service._agent = agent
        request = AgentRequest.model_validate(
            {
                "message": "这个区域有哪些站点",
                "conversationId": conversation.id,
                "sessionId": "session-1",
                "map_context": {
                    "drawn_geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [121.1, 29.8],
                                [122.5, 29.8],
                                [122.5, 30.7],
                                [121.1, 30.7],
                                [121.1, 29.8],
                            ]
                        ],
                        "bbox": [121.1, 29.8, 122.5, 30.7],
                    }
                },
            }
        )

        events = [event async for event in service.stream_chat(request)]

        self.assertIn('"query_type":"polygon"', agent.question)
        self.assertEqual(events[-1]["event"], "done")
        detail = await conversation_repository.get(conversation.id)
        self.assertEqual(detail.messages[0].content, "这个区域有哪些站点")
        self.assertNotIn("selected_area", detail.messages[0].content)
        await service.close()

    async def test_stream_maps_agent_events_and_persists_exchange(self):
        conversation = await conversation_repository.create("测试")
        service = ChatService()
        service._agent = FakeAgent()
        events = [
            event
            async for event in service.stream_chat(
                AgentRequest(question="问题", conversation_id=conversation.id)
            )
        ]
        self.assertEqual(
            [event["event"] for event in events],
            [
                "conversation",
                "process",
                "retrieval",
                "process",
                "token",
                "token",
                "done",
            ],
        )
        self.assertEqual(events[2]["data"]["chunks"][0]["preview"], "检索片段")
        self.assertEqual(events[3]["data"]["status"], "success")
        self.assertEqual(events[1]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["details"]["result_count"], 1)
        self.assertEqual(events[1]["data"]["agent_id"], "agent-loop")
        self.assertEqual(events[-1]["data"]["answer"], "你好[[cite:1]]世界")
        self.assertEqual(events[-1]["data"]["sources"][0]["citation_id"], 1)
        detail = await conversation_repository.get(conversation.id)
        self.assertEqual([message.role for message in detail.messages], ["user", "assistant"])
        self.assertEqual(detail.messages[-1].sources[0]["preview"], "检索片段")

    async def test_answer_agent_buffers_citation_until_tool_round_finishes(self):
        class DelayedLlm:
            model = "fake-model"

            def __init__(self):
                self.call_count = 0
                self.release = asyncio.Event()

            async def stream(self, _messages, tools=None, temperature=0):
                del temperature
                self.call_count += 1
                if self.call_count == 1:
                    yield "", [
                        {
                            "id": "call-rag",
                            "name": "rag_search",
                            "args": {"query": "测试引用"},
                            "type": "tool_call",
                        }
                    ]
                    return
                self.assert_tools_bound(tools)
                yield "第一段<<SRC_", []
                await self.release.wait()
                yield "1>>第二段", []

            @staticmethod
            def assert_tools_bound(tools):
                if not tools:
                    raise AssertionError("最终回答必须由 Answer Agent 直接输出")

        async def rag_search(query):
            return {
                "query": query,
                "chunks": [
                    {
                        "text": "引用依据",
                        "source": "资料.pdf",
                        "score": 0.9,
                        "page_index": 0,
                    }
                ],
            }

        llm = DelayedLlm()
        tools = ToolExecutor()
        tools.register("rag_search", "检索内部文档", rag_search)
        service = ChatService()
        service._agent = AgentLoop(llm, tools)
        stream = encode_chat_stream(
            service.stream_chat(AgentRequest(question="测试流式引用"))
        )

        initial_blocks = [
            await asyncio.wait_for(stream.__anext__(), timeout=1) for _ in range(4)
        ]
        pending_token = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.05)

        self.assertTrue(initial_blocks[0].startswith("event: conversation\n"))
        self.assertFalse(pending_token.done())
        self.assertFalse(llm.release.is_set())

        llm.release.set()
        first_token = await asyncio.wait_for(pending_token, timeout=1)
        self.assertTrue(first_token.startswith("event: token\n"))
        first_payload = json.loads(first_token.split("data: ", 1)[1])
        self.assertEqual(first_payload["data"]["content"], "第一段")

        remaining = [block async for block in stream]
        token_blocks = [
            block for block in remaining if block.startswith("event: token\n")
        ]
        done_block = next(
            block for block in remaining if block.startswith("event: done\n")
        )
        done_payload = json.loads(done_block.split("data: ", 1)[1])

        self.assertEqual(len(token_blocks), 1)
        self.assertIn("[[cite:1]]第二段", token_blocks[0])
        self.assertEqual(
            done_payload["data"]["answer"],
            "第一段[[cite:1]]第二段",
        )
        self.assertEqual(done_payload["data"]["sources"][0]["citation_id"], 1)
        await service.close()

    async def test_map_action_has_run_batch_and_sequence_metadata(self):
        class MapAgent:
            model_name = "fake-model"

            async def run_stream(self, question, history):
                del question, history
                yield AgentEvent(
                    type="map_action",
                    data={
                        "seq": 1,
                        "action_type": "MARKER_ADD",
                        "payload": {"layer_id": "observation-stations", "markers": []},
                    },
                    tool_name="observation_service",
                    tool_call_id="call-map",
                )
                yield AgentEvent(type="answer_delta", content="已显示站点")

        service = ChatService()
        service._agent = MapAgent()

        events = [
            event
            async for event in service.stream_chat(
                AgentRequest(question="显示站点")
            )
        ]

        conversation = events[0]["data"]
        map_action = next(
            event["data"] for event in events if event["event"] == "map_action"
        )
        done = events[-1]["data"]
        self.assertEqual(map_action["run_id"], conversation["run_id"])
        self.assertEqual(map_action["batch_id"], "call-map")
        self.assertEqual(map_action["action_id"], "call-map-1")
        self.assertEqual(map_action["seq"], 1)
        self.assertEqual(map_action["action_type"], "MARKER_ADD")
        self.assertEqual(done["run_id"], conversation["run_id"])
