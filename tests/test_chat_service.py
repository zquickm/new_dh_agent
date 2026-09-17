import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agents.agent_loop import AgentLoop
from agents.events import AgentEvent
from agents.tools.executor import ToolExecutor
from controller.streaming.sse_encoder import encode_chat_stream
from tests.fakes.memory_conversation_repository import conversation_repository
from schemas.chat import AgentRequest
from schemas.skill import ActiveSkillSnapshot
from services.chat_service import ChatService
from services.sensitive_word_service import SensitiveWordMatcher


TEST_USER_ID = "101"


class FakeAgent:
    model_name = "fake-model"

    async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
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
    async def test_next_exchange_filters_disabled_skill_but_keeps_other_results(self):
        from tests.test_agent_loop import FakeLlm

        provider = AsyncMock()
        provider.active_snapshot.side_effect = [
            ActiveSkillSnapshot(enabled_skill_names=frozenset({"断面业务", "空间联动"})),
            ActiveSkillSnapshot(enabled_skill_names=frozenset({"空间联动"})),
        ]
        tools = ToolExecutor()
        tools.register("load_skill", "加载说明", lambda name: f"{name}内部完整说明")
        tools.register("database_query", "业务查询", lambda: "原业务查询结果")
        calls = [
            {"id": "a", "name": "load_skill", "args": {"name": "断面业务"}},
            {"id": "b", "name": "load_skill", "args": {"name": "空间联动"}},
            {"id": "c", "name": "database_query", "args": {}},
        ]
        llm = FakeLlm([([], calls), (["先前最终回答"], []), (["后续回答"], [])])
        service = ChatService(skill_provider=provider)
        service._agent = AgentLoop(llm, tools)
        try:
            first = [event async for event in service.stream_chat(
                AgentRequest(question="初次问题"), user_id=TEST_USER_ID,
            )]
            conversation_id = first[-1]["data"]["conversation_id"]
            before = await conversation_repository.get(conversation_id, user_id=TEST_USER_ID)
            original = before.messages[1].model_messages
            _ = [event async for event in service.stream_chat(
                AgentRequest(question="继续提问", conversation_id=conversation_id),
                user_id=TEST_USER_ID,
            )]
            messages = llm.calls[2][0]
            self.assertNotIn("断面业务内部完整说明", str(messages))
            self.assertIn("空间联动内部完整说明", str(messages))
            self.assertIn("原业务查询结果", str(messages))
            self.assertIn("先前最终回答", str(messages))
            after = await conversation_repository.get(conversation_id, user_id=TEST_USER_ID)
            self.assertEqual(after.messages[1].model_messages, original)
            self.assertEqual(provider.active_snapshot.await_count, 2)
        finally:
            await service.close()

    async def test_parallel_runs_keep_their_starting_skill_snapshot(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        seen = {}

        class CapturingAgent:
            model_name = "fake-model"

            async def run_stream(self, question, history, *, enabled_skill_names):
                seen[question] = enabled_skill_names
                if question == "第一个任务":
                    first_started.set()
                    await release_first.wait()
                    seen["第一个任务结束"] = enabled_skill_names
                yield AgentEvent(type="answer_delta", content="完成")

        provider = AsyncMock()
        provider.active_snapshot.side_effect = [
            ActiveSkillSnapshot(enabled_skill_names=frozenset({"断面业务"})),
            ActiveSkillSnapshot(enabled_skill_names=frozenset()),
        ]
        service = ChatService(skill_provider=provider)
        service._agent = CapturingAgent()

        async def consume(question):
            return [event async for event in service.stream_chat(
                AgentRequest(question=question), user_id=TEST_USER_ID,
            )]

        first = asyncio.create_task(consume("第一个任务"))
        try:
            await asyncio.wait_for(first_started.wait(), 3)
            second = await asyncio.wait_for(consume("第二个任务"), 3)
            self.assertEqual(second[-1]["event"], "done")
            self.assertEqual(seen["第二个任务"], frozenset())
            release_first.set()
            await asyncio.wait_for(first, 3)
            self.assertEqual(seen["第一个任务结束"], frozenset({"断面业务"}))
        finally:
            release_first.set()
            await asyncio.gather(first, return_exceptions=True)
            await service.close()

    async def test_skill_database_failure_does_not_start_agent(self):
        provider = AsyncMock()
        provider.active_snapshot.side_effect = RuntimeError("Skill 数据库不可用")
        service = ChatService(skill_provider=provider)
        try:
            with self.assertRaisesRegex(RuntimeError, "Skill 数据库不可用"):
                _ = [event async for event in service.stream_chat(
                    AgentRequest(question="数据库故障"), user_id=TEST_USER_ID,
                )]
            self.assertIsNone(service._agent)
            self.assertFalse(service._background_tasks)
        finally:
            await service.close()

    async def test_follow_up_replays_persisted_skill_and_parallel_tool_results(self):
        from tests.test_agent_loop import FakeLlm
        from langchain_core.messages import convert_to_messages

        tools = ToolExecutor()
        instructions = "盐度属于海水化学；使用断面专题分析出图。"
        tools.register("load_skill", "加载能力", lambda name: instructions)

        def failing_query():
            raise ValueError("查询失败")

        tools.register("query", "查询", failing_query)
        calls = [
            {"id": "skill-1", "name": "load_skill", "args": {"name": "断面业务"}},
            {"id": "query-1", "name": "query", "args": {}},
        ]
        llm = FakeLlm([([], calls), (["请重试查询"], [])])
        first = ChatService()
        first._agent = AgentLoop(llm, tools)
        events = [event async for event in first.stream_chat(
            AgentRequest(question="查看盐度"), user_id=TEST_USER_ID,
        )]
        self.assertEqual(events[-1]["event"], "done")
        conversation_id = events[-1]["data"]["conversation_id"]
        await first.close()

        # A new service instance must recover history from storage, not memory.
        follow_up = ChatService()
        follow_llm = FakeLlm([(["继续处理"], [])])
        follow_up._agent = AgentLoop(follow_llm, ToolExecutor())
        try:
            events = [event async for event in follow_up.stream_chat(
                AgentRequest(question="继续出图", conversation_id=conversation_id),
                user_id=TEST_USER_ID,
            )]
            self.assertEqual(events[-1]["event"], "done")
            messages = follow_llm.calls[0][0]
            self.assertEqual([m["role"] for m in messages],
                             ["system", "user", "assistant", "tool", "tool", "assistant", "user"])
            self.assertEqual(messages[2]["tool_calls"][0]["args"], {"name": "断面业务"})
            self.assertEqual(messages[3]["content"], instructions)
            self.assertEqual(messages[4]["content"], "工具执行失败")
            self.assertEqual([m["tool_call_id"] for m in messages[3:5]], ["skill-1", "query-1"])
            converted = convert_to_messages(messages)
            self.assertEqual(converted[2].tool_calls[0]["id"], converted[3].tool_call_id)
            saved = await conversation_repository.get(conversation_id, user_id=TEST_USER_ID)
            self.assertIsNotNone(saved.messages[1].model_messages)
            self.assertNotIn("model_messages", saved.model_dump()["messages"][1])
        finally:
            await follow_up.close()

    def setUp(self):
        repository_patch = patch(
            "services.chat_service.conversation_repository", conversation_repository
        )
        repository_patch.start()
        self.addCleanup(repository_patch.stop)
        synonym_patch = patch(
            "services.context_service.expand_query_synonyms", side_effect=lambda value: value
        )
        synonym_patch.start()
        self.addCleanup(synonym_patch.stop)

    @staticmethod
    def _sensitive_word_matcher(_directory: str) -> SensitiveWordMatcher:
        return SensitiveWordMatcher(
            [{"word": "测试敏感词一", "applyScope": "CHAT_INPUT"}]
        )

    async def test_sensitive_input_streams_fixed_answer_without_calling_agent(self):
        class CountingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.calls = 0

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del question, history, run_id
                self.calls += 1
                yield AgentEvent(type="answer_delta", content="不应返回")

        with tempfile.TemporaryDirectory() as directory:
            agent = CountingAgent()
            service = ChatService()
            service._agent = agent
            service._sensitive_word_matcher = self._sensitive_word_matcher(directory)

            events = [
                event
                async for event in service.stream_chat(
                    AgentRequest(question="这里包含测试敏感词一"),
                    user_id=TEST_USER_ID,
                )
            ]

            self.assertEqual(
                [event["event"] for event in events],
                ["conversation", "token", "done"],
            )
            self.assertEqual(
                events[1]["data"]["content"],
                "您的输入包含敏感词，请修改后重新提问。",
            )
            self.assertEqual(
                events[2]["data"],
                {
                    "conversation_id": events[0]["data"]["conversation_id"],
                    "run_id": events[0]["data"]["run_id"],
                    "answer": "您的输入包含敏感词，请修改后重新提问。",
                    "model": "sensitive-word-filter",
                    "sources": [],
                    "images": [],
                    "assistant_message_id": events[2]["data"]["assistant_message_id"],
                },
            )
            self.assertIsNotNone(events[2]["data"]["assistant_message_id"])
            self.assertEqual(agent.calls, 0)
            detail = await conversation_repository.get(
                events[0]["data"]["conversation_id"],
                user_id=TEST_USER_ID,
            )
            self.assertEqual(detail.message_count, 2)
            self.assertEqual(
                [message.role for message in detail.messages],
                ["user", "assistant"],
            )
            self.assertEqual(detail.messages[-1].status, "failed")
            self.assertEqual(
                detail.messages[-1].error_code, "SENSITIVE_INPUT_REJECTED"
            )
            run = await conversation_repository.get_run(
                detail.id, events[0]["data"]["run_id"], user_id=TEST_USER_ID
            )
            self.assertEqual(run.status, "rejected")
            await service.close()

    async def test_generation_continues_after_stream_subscriber_disconnects(self):
        class PausedAgent:
            model_name = "fake-model"

            def __init__(self):
                self.release = asyncio.Event()

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del question, history, run_id
                await self.release.wait()
                yield AgentEvent(type="answer_delta", content="断线后完成")

        conversation = await conversation_repository.create(
            "断线恢复测试", user_id=TEST_USER_ID
        )
        agent = PausedAgent()
        service = ChatService()
        service._agent = agent
        stream = service.stream_chat(
            AgentRequest(question="请继续生成", conversation_id=conversation.id),
            user_id=TEST_USER_ID,
        )

        first_event = await stream.__anext__()
        self.assertEqual(first_event["event"], "conversation")
        await stream.aclose()

        persisted = await conversation_repository.get(
            conversation.id, user_id=TEST_USER_ID
        )
        self.assertEqual(persisted.message_count, 2)
        self.assertEqual(persisted.messages[-1].status, "streaming")

        agent.release.set()
        for _ in range(20):
            await asyncio.sleep(0.01)
            persisted = await conversation_repository.get(
                conversation.id, user_id=TEST_USER_ID
            )
            if persisted.messages[-1].status == "completed":
                break

        self.assertEqual(persisted.messages[-1].content, "断线后完成")
        self.assertEqual(persisted.messages[-1].status, "completed")
        run_id = first_event["data"]["run_id"]
        replayed, status = await conversation_repository.list_run_events(
            conversation.id,
            run_id,
            after=first_event["id"],
            user_id=TEST_USER_ID,
        )
        self.assertEqual(status, "completed")
        self.assertEqual([event.event for event in replayed], ["token", "done"])
        self.assertEqual([event.id for event in replayed], [2, 3])
        await service.close()

    async def test_agent_failure_persists_run_error_details(self):
        class FailingAgent:
            model_name = "failed-model"

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del question, history, run_id
                raise RuntimeError("password=do-not-store upstream unavailable")
                yield

        conversation = await conversation_repository.create(
            "失败持久化测试", user_id=TEST_USER_ID
        )
        service = ChatService()
        service._agent = FailingAgent()

        events = [
            event
            async for event in service.stream_chat(
                AgentRequest(question="触发失败", conversation_id=conversation.id),
                user_id=TEST_USER_ID,
            )
        ]

        run_id = events[0]["data"]["run_id"]
        run = await conversation_repository.get_run(
            conversation.id, run_id, user_id=TEST_USER_ID
        )
        detail = await conversation_repository.get(
            conversation.id, user_id=TEST_USER_ID
        )
        self.assertEqual([event["event"] for event in events], ["conversation", "error"])
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.model, "failed-model")
        self.assertEqual(run.error_code, "AGENT_STREAM_FAILED")
        self.assertEqual(run.error_type, "RuntimeError")
        self.assertNotIn("do-not-store", run.error_message)
        self.assertEqual(detail.messages[-1].status, "failed")
        await service.close()

    async def test_sensitive_input_does_not_persist_map_context(self):
        with tempfile.TemporaryDirectory() as directory:
            conversation = await conversation_repository.create(
                "敏感词测试", user_id=TEST_USER_ID
            )
            service = ChatService()
            service._agent = FakeAgent()
            service._sensitive_word_matcher = self._sensitive_word_matcher(directory)
            request = AgentRequest.model_validate(
                {
                    "question": "测试敏感词一",
                    "conversation_id": conversation.id,
                    "map_context": {
                        "drawn_geometry": {
                            "type": "Polygon",
                            "coordinates": [[
                                [121.1, 29.8],
                                [122.5, 29.8],
                                [122.5, 30.7],
                                [121.1, 29.8],
                            ]],
                            "bbox": [121.1, 29.8, 122.5, 30.7],
                        }
                    },
                }
            )

            events = [
                event
                async for event in service.stream_chat(
                    request, user_id=TEST_USER_ID
                )
            ]

            self.assertEqual([event["event"] for event in events], [
                "conversation",
                "token",
                "done",
            ])
            self.assertIsNone(
                await conversation_repository.get_map_context(
                    conversation.id, user_id=TEST_USER_ID
                )
            )
            detail = await conversation_repository.get(
                conversation.id, user_id=TEST_USER_ID
            )
            self.assertEqual(detail.message_count, 2)
            self.assertEqual(detail.messages[-1].status, "failed")

    def test_all_registered_tool_names_have_chinese_display_names(self):
        self.assertEqual(
            {
                name: ChatService._tool_display_name(name)
                for name in (
                    "load_skill",
                    "CurrentServerTime",
                    "rag_search",
                    "database_query",
                    "observation_service",
                    "map_action",
                    "dm_duanmian_analysis",
                )
            },
            {
                "load_skill": "能力加载",
                "CurrentServerTime": "当前服务器时间",
                "rag_search": "东溟知识库检索",
                "database_query": "数据库查询",
                "observation_service": "观测分析",
                "map_action": "地图操作",
                "dm_duanmian_analysis": "断面专题分析",
            },
        )

    async def test_frontend_geometry_reaches_agent_but_is_not_persisted_in_question(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.question = ""

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del history, run_id
                self.question = question
                self.frontend_context = frontend_context
                yield AgentEvent(
                    type="answer_delta",
                    content="区域内有观测站",
                    round_number=1,
                )

        conversation = await conversation_repository.create("地图选区测试", user_id=TEST_USER_ID)
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

        events = [event async for event in service.stream_chat(request, user_id=TEST_USER_ID)]

        self.assertEqual(agent.question, request.question)
        self.assertEqual(agent.frontend_context, request.context)
        self.assertEqual(events[-1]["event"], "done")
        detail = await conversation_repository.get(conversation.id, user_id=TEST_USER_ID)
        self.assertEqual(detail.messages[0].content, "这个区域有哪些站点")
        self.assertNotIn("selected_area", detail.messages[0].content)
        await service.close()

    async def test_conversation_inherits_and_replaces_map_area(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.questions = []
                self.contexts = []

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del history, run_id
                self.questions.append(question)
                self.contexts.append(frontend_context)
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

        conversation = await conversation_repository.create("选区继承测试", user_id=TEST_USER_ID)
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
                question="具体有哪些雷达站",
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
                question="其中有哪些浮标站",
                conversation_id=conversation.id,
            ),
        ]
        for request in requests:
            events = [event async for event in service.stream_chat(request, user_id=TEST_USER_ID)]
            self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(agent.contexts, [
            requests[0].context, requests[0].context,
            requests[2].context, requests[2].context,
        ])
        self.assertEqual(
            await conversation_repository.get_map_context(conversation.id, user_id=TEST_USER_ID),
            requests[2].context,
        )
        self.assertEqual(agent.questions, [request.question for request in requests])
        detail = await conversation_repository.get(conversation.id, user_id=TEST_USER_ID)
        self.assertTrue(
            all("selected_area" not in message.content for message in detail.messages)
        )
        await service.close()

    async def test_conversation_switches_to_circle_and_inherits_it(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.questions = []
                self.contexts = []

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del history, run_id
                self.questions.append(question)
                self.contexts.append(frontend_context)
                yield AgentEvent(type="answer_delta", content="回答")

        conversation = await conversation_repository.create("圆形选区测试", user_id=TEST_USER_ID)
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
            _ = [event async for event in service.stream_chat(request, user_id=TEST_USER_ID)]

        self.assertEqual(agent.contexts[0], requests[0].context)
        self.assertEqual(agent.contexts[1], requests[1].context)
        self.assertEqual(agent.contexts[2], requests[1].context)
        self.assertEqual(agent.questions, [request.question for request in requests])

    async def test_single_station_follow_up_uses_history_without_reusing_map_area(self):
        class CapturingAgent:
            model_name = "fake-model"

            def __init__(self):
                self.calls = []

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del run_id
                self.calls.append((question, history))
                answers = ["区域内有小洋山站", "小洋山站运行正常", "坐标已查询"]
                yield AgentEvent(
                    type="answer_delta",
                    content=answers[len(self.calls) - 1],
                )

        conversation = await conversation_repository.create("单站指代测试", user_id=TEST_USER_ID)
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
            _ = [event async for event in service.stream_chat(request, user_id=TEST_USER_ID)]

        question, history = agent.calls[2]
        self.assertNotIn("selected_area", question)
        self.assertEqual(question, requests[2].question)
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
                self.contexts = []

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                del history, run_id
                self.questions.append(question)
                self.contexts.append(frontend_context)
                yield AgentEvent(type="answer_delta", content="回答")

        first = await conversation_repository.create("选区会话", user_id=TEST_USER_ID)
        second = await conversation_repository.create("普通会话", user_id=TEST_USER_ID)
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
        _ = [event async for event in service.stream_chat(first_request, user_id=TEST_USER_ID)]
        _ = [
            event
            async for event in service.stream_chat(
                AgentRequest(question="该区域呢", conversation_id=second.id),
                user_id=TEST_USER_ID,
            )
        ]

        self.assertEqual(agent.contexts[0], first_request.context)
        self.assertIsNone(agent.contexts[1])

    async def test_database_citation_streams_and_persists_without_model_marker(self):
        from tests.test_agent_loop import FakeLlm

        async def query(question):
            return {"success": True, "rows": [{"平均水温": 20.5}]}

        executor = ToolExecutor()
        executor.register("database_query", "查询", query)
        llm = FakeLlm([
            ([], [{"id": "db-1", "name": "database_query",
                   "args": {"question": "水温统计"}, "type": "tool_call"}]),
            (["平均水温为20.5℃。"], []),
            (["你好。"], []),
        ])
        service = ChatService()
        service._agent = AgentLoop(llm, executor)
        conversation = await conversation_repository.create("测试", user_id=TEST_USER_ID)
        events = [event async for event in service.stream_chat(
            AgentRequest(question="水温统计", conversation_id=conversation.id),
            user_id=TEST_USER_ID,
        )]
        done = events[-1]["data"]
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(done["answer"], "平均水温为20.5℃。")
        self.assertEqual("".join(event["data"]["content"] for event in events
                                 if event["event"] == "token"), done["answer"])
        self.assertEqual(done["sources"][0]["source_type"], "database")
        detail = await conversation_repository.get(conversation.id, user_id=TEST_USER_ID)
        self.assertEqual(detail.messages[-1].sources, done["sources"])
        self.assertEqual(detail.messages[-1].content, done["answer"])
        next_events = [event async for event in service.stream_chat(
            AgentRequest(question="你好", conversation_id=conversation.id),
            user_id=TEST_USER_ID,
        )]
        self.assertEqual(next_events[-1]["data"]["sources"], [])
        self.assertEqual(next_events[-1]["data"]["answer"], "你好。")
        await service._agent.close()

    async def test_explicit_article_without_marker_is_returned_and_persisted(self):
        class NamedArticleAgent(FakeAgent):
            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
                async for event in super().run_stream(question, history, run_id):
                    if event.type != "answer_delta":
                        yield event
                yield AgentEvent(type="answer_delta", content="根据《资料》中的内容回答。")

        conversation = await conversation_repository.create("测试", user_id=TEST_USER_ID)
        service = ChatService()
        service._agent = NamedArticleAgent()
        events = [event async for event in service.stream_chat(
            AgentRequest(question="查询资料", conversation_id=conversation.id),
            user_id=TEST_USER_ID,
        )]
        self.assertEqual(events[-1]["event"], "done")
        done = events[-1]["data"]
        self.assertEqual(done["answer"], "根据《资料》中的内容回答。")
        self.assertEqual([item["source"] for item in done["sources"]], ["资料.pdf"])
        detail = await conversation_repository.get(conversation.id, user_id=TEST_USER_ID)
        self.assertEqual(detail.messages[-1].sources, done["sources"])

    async def test_stream_maps_agent_events_and_persists_exchange(self):
        conversation = await conversation_repository.create("测试", user_id=TEST_USER_ID)
        service = ChatService()
        service._agent = FakeAgent()
        with self.assertLogs("services.chat_service", level="INFO") as captured:
            events = [
                event
                async for event in service.stream_chat(
                    AgentRequest(question="完整用户问题", conversation_id=conversation.id),
                    user_id=TEST_USER_ID,
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
        self.assertEqual(events[1]["data"]["title"], "算法工具箱：东溟知识库检索")
        self.assertEqual(events[1]["data"]["summary"], "东溟知识库检索正在执行")
        self.assertEqual(events[2]["data"]["tool_name"], "东溟知识库检索")
        self.assertEqual(events[3]["data"]["summary"], "东溟知识库检索执行完成")
        self.assertEqual(events[1]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["step_id"], "call-1")
        self.assertEqual(events[3]["data"]["details"]["result_count"], 1)
        self.assertEqual(events[1]["data"]["agent_id"], "agent-loop")
        self.assertEqual(events[-1]["data"]["answer"], "你好[[cite:1]]世界")
        self.assertEqual(events[-1]["data"]["sources"][0]["citation_id"], 1)
        detail = await conversation_repository.get(conversation.id, user_id=TEST_USER_ID)
        self.assertEqual([message.role for message in detail.messages], ["user", "assistant"])
        self.assertEqual(detail.messages[-1].sources[0]["preview"], "检索片段")
        run = await conversation_repository.get_run(
            conversation.id, events[0]["data"]["run_id"], user_id=TEST_USER_ID
        )
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.model, "fake-model")
        self.assertIsNotNone(run.finished_at)
        self.assertIsNotNone(run.duration_ms)
        self.assertIn("完整用户问题", "\n".join(captured.output))

    async def test_failed_tool_process_exposes_safe_failure_reason(self):
        class FailingAgent:
            model_name = "fake-model"

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
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

        conversation = await conversation_repository.create("失败原因测试", user_id=TEST_USER_ID)
        service = ChatService()
        service._agent = FailingAgent()

        events = [
            event
            async for event in service.stream_chat(
                AgentRequest(
                    question="查询区域站点",
                    conversation_id=conversation.id,
                ),
                user_id=TEST_USER_ID,
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
            service.stream_chat(AgentRequest(question="测试流式引用"), user_id=TEST_USER_ID)
        )

        initial_blocks = []
        first_token = ""
        while not first_token.startswith("event: token\n"):
            block = await asyncio.wait_for(stream.__anext__(), timeout=1)
            if block.startswith("event: token\n"):
                first_token = block
            else:
                initial_blocks.append(block)
        self.assertTrue(initial_blocks[0].startswith("event: conversation\n"))
        self.assertTrue(any(block.startswith("event: context_usage\n") for block in initial_blocks))
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

            async def run_stream(self, question, history, run_id="-", *, frontend_context=None):
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
                AgentRequest(question="显示站点"), user_id=TEST_USER_ID
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
