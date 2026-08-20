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
