import json
import unittest
from copy import deepcopy

from agents.answer_merger import AnswerMerger
from agents.citations import CitationRegistry


class FakeLlm:
    model = "fake-model"

    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = []

    async def stream(self, messages, tools=None, temperature=0):
        del temperature
        self.calls.append((deepcopy(messages), tools))
        for chunk in self.chunks:
            yield chunk, []


class AnswerMergerTest(unittest.IsolatedAsyncioTestCase):
    async def test_forwards_validated_citations_as_real_stream_chunks(self):
        registry = CitationRegistry()
        registry.register_rag_observation(
            json.dumps(
                {
                    "query": "测试",
                    "chunks": [
                        {
                            "text": "来源正文",
                            "source": "资料.pdf",
                            "score": 0.9,
                            "page_index": 1,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )
        llm = FakeLlm(["第一段<<SRC_", "1>>第二段"])
        merger = AnswerMerger(llm)

        chunks = [
            chunk
            async for chunk in merger.stream_merge(
                "问题",
                "候选回答<<SRC_1>>",
                registry,
            )
        ]

        self.assertEqual(chunks, ["第一段", "[[cite:1]]第二段"])
        messages, tools = llm.calls[0]
        self.assertIsNone(tools)
        self.assertIn("来源键：<<SRC_1>>", messages[-1]["content"])
        self.assertIn("第 2 页", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
