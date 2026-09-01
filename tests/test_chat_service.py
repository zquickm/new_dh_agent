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

    async def run_stream(self, question, history, run_id="-"):
        del question, history, run_id
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
    def test_all_registered_tool_names_have_chinese_display_names(self):
        self.assertEqual(
            {
                name: ChatService._tool_display_name(name)
                for name in (
                    "CurrentServerTime",
                    "rag_search",
                    "database_query",
                    "observation_service",
                    "map_action",
                )
            },
            {
                "CurrentServerTime": "当前服务器时间",
                "rag_search": "知识库检索",
                "database_query": "数据库查询",
                "observation_service": "观测分析",
                "map_action": "地图操作",
            },
        )

    async def test_frontend_geometry_reaches_agent_but_is_not_persisted_in_question(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.question = ""

            async def run_stream(self, question, history, run_id="-"):
                del history, run_id
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

    async def test_conversation_reuses_and_overrides_latest_map_area(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.questions = []

            async def run_stream(self, question, history, run_id="-"):
                del history, run_id
                self.questions.append(question)
                yield AgentEvent(type="answer_delta", content="回答")

        def map_context(min_lon, min_lat, max_lon, max_lat):
            return {
                "drawn_geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [min_lon, min_lat],
                        [max_lon, min_lat],
                        [max_lon, max_lat],
                        [min_lon, max_lat],
                        [min_lon, min_lat],
                    ]],
                    "bbox": [min_lon, min_lat, max_lon, max_lat],
                }
            }

        conversation = await conversation_repository.create("选区继承测试")
        agent = CapturingAgent()
        service = ChatService()
        service._agent = agent

        requests = [
            AgentRequest.model_validate(
                {
                    "message": "框选区域内有哪些站点",
                    "conversationId": conversation.id,
                    "map_context": map_context(121.1, 29.8, 122.5, 30.7),
                }
            ),
            AgentRequest(
                question="该区域的站点状态怎么样",
                conversation_id=conversation.id,
            ),
            AgentRequest.model_validate(
                {
                    "message": "改看这个新区域",
                    "conversationId": conversation.id,
                    "map_context": map_context(123.1, 30.8, 124.5, 31.7),
                }
            ),
            AgentRequest(
                question="该区域有哪些站点",
                conversation_id=conversation.id,
            ),
        ]
        for request in requests:
            _ = [event async for event in service.stream_chat(request)]

        self.assertIn('"bbox":[121.1,29.8,122.5,30.7]', agent.questions[1])
        self.assertIn('"bbox":[123.1,30.8,124.5,31.7]', agent.questions[3])
        self.assertNotIn('"bbox":[121.1,29.8,122.5,30.7]', agent.questions[3])
        detail = await conversation_repository.get(conversation.id)
        self.assertTrue(
            all("selected_area" not in message.content for message in detail.messages)
        )

    async def test_conversation_switches_from_polygon_to_circle_and_reuses_circle(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.questions = []

            async def run_stream(self, question, history, run_id="-"):
                del history, run_id
                self.questions.append(question)
                yield AgentEvent(type="answer_delta", content="回答")

        conversation = await conversation_repository.create("圆形选区测试")
        agent = CapturingAgent()
        service = ChatService()
        service._agent = agent

        requests = [
            AgentRequest.model_validate(
                {
                    "message": "矩形区域有哪些站点",
                    "conversationId": conversation.id,
                    "map_context": {
                        "drawn_geometry": {
                            "type": "Polygon",
                            "coordinates": [[
                                [121.1, 29.8],
                                [122.5, 29.8],
                                [122.5, 30.7],
                                [121.1, 30.7],
                                [121.1, 29.8],
                            ]],
                            "bbox": [121.1, 29.8, 122.5, 30.7],
                        }
                    },
                }
            ),
            AgentRequest.model_validate(
                {
                    "message": "改看这个圆形区域",
                    "conversationId": conversation.id,
                    "map_context": {
                        "drawn_geometry": {
                            "query_type": "circle",
                            "center": [123.4, 31.2],
                            "radius": 18000,
                        }
                    },
                }
            ),
            AgentRequest(
                question="只保留这个区域内的雷达站",
                conversation_id=conversation.id,
            ),
        ]
        for request in requests:
            _ = [event async for event in service.stream_chat(request)]

        self.assertIn('"query_type":"polygon"', agent.questions[0])
        self.assertIn('"query_type":"circle"', agent.questions[1])
        self.assertIn('"center":[123.4,31.2]', agent.questions[1])
        self.assertIn('"radius":18000.0', agent.questions[1])
        self.assertIn('"query_type":"circle"', agent.questions[2])
        self.assertNotIn('"query_type":"polygon"', agent.questions[2])
        self.assertIn("雷达、雷达站使用 radar", agent.questions[2])

    async def test_single_station_follow_up_uses_history_without_reusing_map_area(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.calls = []

            async def run_stream(self, question, history, run_id="-"):
                del run_id
                self.calls.append((question, history))
                answers = ["区域内有小洋山站", "小洋山站运行正常", "坐标已查询"]
                yield AgentEvent(
                    type="answer_delta",
                    content=answers[len(self.calls) - 1],
                )

        conversation = await conversation_repository.create("单站指代测试")
        agent = CapturingAgent()
        service = ChatService()
        service._agent = agent
        map_context = {
            "drawn_geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [121.1, 29.8],
                    [122.5, 29.8],
                    [122.5, 30.7],
                    [121.1, 30.7],
                    [121.1, 29.8],
                ]],
                "bbox": [121.1, 29.8, 122.5, 30.7],
            }
        }

        requests = [
            AgentRequest.model_validate(
                {
                    "message": "这个区域有哪些站点",
                    "conversationId": conversation.id,
                    "map_context": map_context,
                }
            ),
            AgentRequest(
                question="小洋山站的详情",
                conversation_id=conversation.id,
            ),
            AgentRequest(
                question="该站点的经纬度是多少",
                conversation_id=conversation.id,
            ),
        ]
        for request in requests:
            _ = [event async for event in service.stream_chat(request)]

        question, history = agent.calls[2]
        self.assertNotIn("selected_area", question)
        self.assertIn("最近一轮明确讨论的单个站点", question)
        self.assertEqual(
            [message.content for message in history],
            [
                "这个区域有哪些站点",
                "区域内有小洋山站",
                "小洋山站的详情",
                "小洋山站运行正常",
            ],
        )

    async def test_map_area_does_not_leak_between_conversations(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.questions = []

            async def run_stream(self, question, history, run_id="-"):
                del history, run_id
                self.questions.append(question)
                yield AgentEvent(type="answer_delta", content="回答")

        first = await conversation_repository.create("选区会话")
        second = await conversation_repository.create("普通会话")
        agent = CapturingAgent()
        service = ChatService()
        service._agent = agent
        context = {
            "drawn_geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [121.1, 29.8],
                    [122.5, 29.8],
                    [122.5, 30.7],
                    [121.1, 30.7],
                    [121.1, 29.8],
                ]],
                "bbox": [121.1, 29.8, 122.5, 30.7],
            }
        }

        first_request = AgentRequest.model_validate(
            {
                "message": "框选区域",
                "conversationId": first.id,
                "map_context": context,
            }
        )
        _ = [event async for event in service.stream_chat(first_request)]
        _ = [
            event
            async for event in service.stream_chat(
                AgentRequest(question="该区域呢", conversation_id=second.id)
            )
        ]

        self.assertIn("selected_area", agent.questions[0])
        self.assertNotIn("selected_area", agent.questions[1])

    async def test_stream_maps_agent_events_and_persists_exchange(self):
        conversation = await conversation_repository.create("测试")
        service = ChatService()
        service._agent = FakeAgent()
        with self.assertLogs("services.chat_service", level="INFO") as captured:
            events = [
                event
                async for event in service.stream_chat(
                    AgentRequest(question="完整用户问题", conversation_id=conversation.id)
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
        self.assertEqual(events[1]["data"]["title"], "算法工具箱：知识库检索")
        self.assertEqual(events[1]["data"]["summary"], "知识库检索正在执行")
        self.assertEqual(events[3]["data"]["summary"], "知识库检索执行完成")
        self.assertEqual(events[1]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["details"]["result_count"], 1)
        self.assertEqual(events[1]["data"]["agent_id"], "agent-loop")
        self.assertEqual(events[-1]["data"]["answer"], "你好[[cite:1]]世界")
        self.assertEqual(events[-1]["data"]["sources"][0]["citation_id"], 1)
        detail = await conversation_repository.get(conversation.id)
        self.assertEqual([message.role for message in detail.messages], ["user", "assistant"])
        self.assertEqual(detail.messages[-1].sources[0]["preview"], "检索片段")
        self.assertIn("完整用户问题", "\n".join(captured.output))

    async def test_failed_tool_process_exposes_safe_failure_reason(self):
        class FailingAgent:
            model_name = "fake-model"

            async def run_stream(self, question, history, run_id="-"):
                del question, history, run_id
                yield AgentEvent(
                    type="tool_started",
                    round_number=1,
                    tool_name="observation_service",
                    tool_call_id="call-observation",
                )
                yield AgentEvent(
                    type="tool_finished",
                    content="观测工具参数无效：query_type 不能为空",
                    round_number=1,
                    tool_name="observation_service",
                    tool_call_id="call-observation",
                    success=False,
                )
                yield AgentEvent(type="answer_delta", content="请重新选择区域")

        conversation = await conversation_repository.create("失败原因测试")
        service = ChatService()
        service._agent = FailingAgent()

        events = [
            event
            async for event in service.stream_chat(
                AgentRequest(
                    question="查询区域站点",
                    conversation_id=conversation.id,
                )
            )
        ]

        failed_process = next(
            event
            for event in events
            if event["event"] == "process"
            and event["data"]["status"] == "failed"
        )
        self.assertEqual(
            failed_process["data"]["summary"],
            "观测工具参数无效：query_type 不能为空",
        )

    async def test_answer_agent_streams_citation_before_completion(self):
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
                yield "第一段<<SRC_", []
                await self.release.wait()
                yield "1>>第二段", []

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
        self.assertTrue(initial_blocks[0].startswith("event: conversation\n"))
        first_token = await asyncio.wait_for(stream.__anext__(), timeout=1)
        self.assertFalse(llm.release.is_set())
        self.assertTrue(first_token.startswith("event: token\n"))
        first_payload = json.loads(first_token.split("data: ", 1)[1])
        self.assertEqual(first_payload["data"]["content"], "第一段")

        llm.release.set()
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

            async def run_stream(self, question, history, run_id="-"):
                del question, history, run_id
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
