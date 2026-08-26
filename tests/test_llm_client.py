import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessageChunk

from config.settings import clear_settings_cache


class LlmClientTest(unittest.TestCase):
    def tearDown(self):
        clear_settings_cache()

    def test_uses_placeholder_when_upstream_does_not_require_api_key(self):
        with patch.dict(
            os.environ,
            {"NEW_DH_LLM__API_KEY": ""},
            clear=False,
        ):
            clear_settings_cache()
            with patch("agents.llm_client.ChatOpenAI") as chat_openai:
                from agents.llm_client import HelloAgentsLLM

                HelloAgentsLLM()

            self.assertEqual(chat_openai.call_args.kwargs["api_key"], "not-required")

    def test_passes_configured_api_key(self):
        with patch.dict(
            os.environ,
            {"NEW_DH_LLM__API_KEY": "configured-key"},
            clear=False,
        ):
            clear_settings_cache()
            with patch("agents.llm_client.ChatOpenAI") as chat_openai:
                from agents.llm_client import HelloAgentsLLM

                HelloAgentsLLM()

            self.assertEqual(chat_openai.call_args.kwargs["api_key"], "configured-key")


class LlmClientToolCallTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def observation_tools():
        return [
            {
                "type": "function",
                "function": {
                    "name": "observation_service",
                    "description": "查询观测站",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def test_dsml_stream_guard_never_releases_a_partial_opener(self):
        from agents.llm_client import DSMLParseError, DSMLStreamGuard

        guard = DSMLStreamGuard()

        self.assertEqual(guard.feed("普通正文<|DSM"), "普通正文")
        with self.assertRaises(DSMLParseError):
            guard.finish()

    async def test_returns_content_and_native_tool_calls(self):
        from agents.llm_client import HelloAgentsLLM

        tool_calls = [
            {
                "id": "call-1",
                "name": "database_query",
                "args": {"question": "查询E2断面盐度"},
                "type": "tool_call",
            }
        ]
        response = SimpleNamespace(content="正在查询", tool_calls=tool_calls)

        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            bound_model = chat_openai.return_value.bind_tools.return_value
            bound_model.ainvoke = AsyncMock(return_value=response)
            llm = HelloAgentsLLM()

            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "database_query",
                        "description": "查询实际数据",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ]
            result = await llm.invoke(
                [{"role": "user", "content": "查询E2断面盐度"}],
                tools,
            )

        chat_openai.return_value.bind_tools.assert_called_once_with(tools)
        bound_model.ainvoke.assert_awaited_once_with(
            [{"role": "user", "content": "查询E2断面盐度"}],
            temperature=0,
        )
        self.assertEqual(
            result,
            ("正在查询", tool_calls),
        )

    async def test_streams_content_and_combines_native_tool_calls(self):
        from agents.llm_client import HelloAgentsLLM

        async def chunks(*args, **kwargs):
            del args, kwargs
            yield AIMessageChunk(content="正在")
            yield AIMessageChunk(content="查询")
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": "rag_search",
                        "args": '{"query":"E2',
                        "id": "call-1",
                        "index": 0,
                    }
                ],
            )
            yield AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": None,
                        "args": '断面"}',
                        "id": None,
                        "index": 0,
                    }
                ],
            )

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "rag_search",
                    "description": "检索内部知识库",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            bound_model = chat_openai.return_value.bind_tools.return_value
            bound_model.astream = chunks
            llm = HelloAgentsLLM()

            result = [
                item
                async for item in llm.stream(
                    [{"role": "user", "content": "查询E2断面"}], tools
                )
            ]

        chat_openai.return_value.bind_tools.assert_called_once_with(tools)
        self.assertEqual(
            result,
            [
                ("正在", []),
                ("查询", []),
                (
                    "",
                    [
                        {
                            "name": "rag_search",
                            "args": {"query": "E2断面"},
                            "id": "call-1",
                            "type": "tool_call",
                        }
                    ],
                ),
            ],
        )

    async def test_converts_streamed_deepseek_dsml_to_tool_calls(self):
        from agents.llm_client import HelloAgentsLLM

        async def chunks(*args, **kwargs):
            del args, kwargs
            yield AIMessageChunk(content="<｜DSML｜tool_")
            yield AIMessageChunk(
                content=(
                    'calls>\n<｜DSML｜invoke name="observation_service">\n'
                    '<｜DSML｜parameter name="operation" string="true">'
                    'search_stations</｜DSML｜parameter>\n'
                    '<｜DSML｜parameter name="station_types" string="false">'
                    '["shore_station"]</｜DSML｜parameter>\n'
                )
            )
            yield AIMessageChunk(
                content=(
                    '<｜DSML｜parameter name="query_type" string="true">'
                    'bbox</｜DSML｜parameter>\n'
                    '<｜DSML｜parameter name="bbox" string="false">'
                    '[100, 0, 130, 45]</｜DSML｜parameter>\n'
                    '</｜DSML｜invoke>\n</｜DSML｜tool_calls>'
                )
            )

        tools = self.observation_tools()
        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            bound_model = chat_openai.return_value.bind_tools.return_value
            bound_model.astream = chunks
            llm = HelloAgentsLLM()

            result = [
                item
                async for item in llm.stream(
                    [{"role": "user", "content": "现在哪里浪最大"}], tools
                )
            ]

        self.assertEqual(len(result), 1)
        content, tool_calls = result[0]
        self.assertEqual(content, "")
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "observation_service")
        self.assertEqual(
            tool_calls[0]["args"],
            {
                "operation": "search_stations",
                "station_types": ["shore_station"],
                "query_type": "bbox",
                "bbox": [100, 0, 130, 45],
            },
        )

    async def test_rejects_incomplete_dsml_instead_of_streaming_it_as_answer(self):
        from agents.llm_client import HelloAgentsLLM, LLMInvocationError

        async def chunks(*args, **kwargs):
            del args, kwargs
            yield AIMessageChunk(
                content=(
                    '<|DSML|tool_calls><|DSML|invoke name="observation_service">'
                )
            )

        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            bound_model = chat_openai.return_value.bind_tools.return_value
            bound_model.astream = chunks
            llm = HelloAgentsLLM()

            with self.assertRaises(LLMInvocationError):
                _ = [
                    item
                    async for item in llm.stream(
                        [{"role": "user", "content": "查询观测站"}],
                        self.observation_tools(),
                    )
                ]

    async def test_rejects_dsml_calls_to_unregistered_tools(self):
        from agents.llm_client import HelloAgentsLLM, LLMInvocationError

        async def chunks(*args, **kwargs):
            del args, kwargs
            yield AIMessageChunk(
                content=(
                    '<|DSML|tool_calls><|DSML|invoke name="unknown_tool">'
                    '{}'
                    '</|DSML|invoke></|DSML|tool_calls>'
                )
            )

        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            bound_model = chat_openai.return_value.bind_tools.return_value
            bound_model.astream = chunks
            llm = HelloAgentsLLM()

            with self.assertRaises(LLMInvocationError):
                _ = [
                    item
                    async for item in llm.stream(
                        [{"role": "user", "content": "查询观测站"}],
                        self.observation_tools(),
                    )
                ]

    async def test_tool_free_stream_does_not_accumulate_the_full_response(self):
        from agents.llm_client import HelloAgentsLLM

        class StreamingChunk:
            def __init__(self, content):
                self.content = content

            def __add__(self, _other):
                raise AssertionError("tool-free answer chunks must not be accumulated")

        async def chunks(*args, **kwargs):
            del args, kwargs
            yield StreamingChunk("第一段")
            yield StreamingChunk("第二段")

        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            chat_openai.return_value.astream = chunks
            llm = HelloAgentsLLM()

            result = [
                item
                async for item in llm.stream(
                    [{"role": "user", "content": "直接回答"}],
                    None,
                )
            ]

        self.assertEqual(result, [("第一段", []), ("第二段", [])])

    async def test_tool_bound_answer_stream_does_not_accumulate_content(self):
        from agents.llm_client import HelloAgentsLLM

        class StreamingChunk:
            tool_call_chunks = []
            tool_calls = []

            def __init__(self, content):
                self.content = content

            def __add__(self, _other):
                raise AssertionError("answer content chunks must not be accumulated")

        async def chunks(*args, **kwargs):
            del args, kwargs
            yield StreamingChunk("第一段")
            yield StreamingChunk("第二段")

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "rag_search",
                    "description": "检索内部知识库",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with patch("agents.llm_client.ChatOpenAI") as chat_openai:
            bound_model = chat_openai.return_value.bind_tools.return_value
            bound_model.astream = chunks
            llm = HelloAgentsLLM()

            result = [
                item
                async for item in llm.stream(
                    [{"role": "user", "content": "直接回答"}],
                    tools,
                )
            ]

        self.assertEqual(result, [("第一段", []), ("第二段", [])])
