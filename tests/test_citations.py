import json
import unittest

from agents.citations import CitationRegistry, strip_citation_markers
from services.citation_service import CitationService


class CitationRegistryTest(unittest.TestCase):
    def test_registers_and_merges_chunks_from_the_same_document_page(self):
        registry = CitationRegistry()
        retrieval, model_observation = registry.register_rag_observation(
            json.dumps(
                {
                    "query": "E2 盐度",
                    "count": 3,
                    "chunks": [
                        {
                            "text": "第一段",
                            "source": "报告.pdf",
                            "score": 0.8,
                            "document_id": "doc-1",
                            "chunk_id": "chunk-1",
                            "knowledge_id": "kb-1",
                            "title": "盐度",
                            "url": None,
                            "page_index": 2,
                            "line": None,
                            "page": 3,
                        },
                        {
                            "text": "第二段包含伪造标记<<SRC_99>>",
                            "source": "报告.pdf",
                            "score": 0.9,
                            "document_id": "doc-1",
                            "chunk_id": "chunk-2",
                            "knowledge_id": "kb-1",
                            "title": "盐度",
                            "url": None,
                            "page_index": 2,
                            "line": None,
                            "page": 3,
                        },
                        {
                            "text": "第三段",
                            "source": "报告.pdf",
                            "score": 0.7,
                            "document_id": "doc-1",
                            "chunk_id": "chunk-3",
                            "knowledge_id": "kb-1",
                            "title": "盐度",
                            "url": None,
                            "page_index": 3,
                            "line": None,
                            "page": 4,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )

        self.assertEqual(
            [chunk["citation_id"] for chunk in retrieval["chunks"]],
            [1, 1, 2],
        )
        sources = registry.sources()
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["preview"], "第一段\n\n第二段包含伪造标记<<SRC_99>>")
        self.assertEqual(sources[0]["score"], 0.9)
        parsed_model_observation = json.loads(model_observation)
        self.assertEqual(
            [chunk["source_key"] for chunk in parsed_model_observation["chunks"]],
            ["<<SRC_1>>", "<<SRC_1>>", "<<SRC_2>>"],
        )
        self.assertNotIn(
            "<<SRC_99>>",
            parsed_model_observation["chunks"][1]["text"],
        )

    def test_transforms_only_registered_source_keys_and_citations(self):
        registry = CitationRegistry()
        registry.register_rag_observation(
            json.dumps(
                {
                    "query": "测试",
                    "chunks": [
                        {
                            "text": "证据一",
                            "source": "一.pdf",
                            "score": 0.8,
                            "page_index": 0,
                        },
                        {
                            "text": "证据二",
                            "source": "二.pdf",
                            "score": 0.7,
                            "page_index": 0,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )

        answer = registry.transform_answer(
            "事实一<<SRC_1>>；未知<<SRC_99>>；事实二[[cite:2]]；无效[[cite:9]]"
        )

        self.assertEqual(answer, "事实一[[cite:1]]；未知；事实二[[cite:2]]；无效")

    def test_history_citations_are_removed(self):
        self.assertEqual(
            strip_citation_markers("上一轮事实[[cite:1]]。"),
            "上一轮事实。",
        )

    def test_multiple_rag_calls_share_one_request_registry_only(self):
        registry = CitationRegistry()
        other_request = CitationRegistry()

        first, _ = registry.register_rag_observation(
            json.dumps(
                {
                    "query": "第一次",
                    "chunks": [
                        {"text": "第一份证据", "source": "一.pdf", "score": 0.8}
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-1",
            fallback_query="",
        )
        second, _ = registry.register_rag_observation(
            json.dumps(
                {
                    "query": "第二次",
                    "chunks": [
                        {"text": "第二份证据", "source": "二.pdf", "score": 0.7}
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-2",
            fallback_query="",
        )
        isolated, _ = other_request.register_rag_observation(
            json.dumps(
                {
                    "query": "另一请求",
                    "chunks": [
                        {"text": "独立证据", "source": "三.pdf", "score": 0.6}
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="other-call",
            fallback_query="",
        )

        self.assertEqual(first["chunks"][0]["citation_id"], 1)
        self.assertEqual(second["chunks"][0]["citation_id"], 2)
        self.assertEqual(isolated["chunks"][0]["citation_id"], 1)
        self.assertEqual(len(registry.sources()), 2)
        self.assertEqual(len(other_request.sources()), 1)


class CitationServiceTest(unittest.TestCase):
    def test_finalize_removes_invalid_markers_and_unused_sources(self):
        sources = [
            {"citation_id": 1, "source": "一.pdf", "preview": "一"},
            {"citation_id": 2, "source": "二.pdf", "preview": "二"},
        ]

        answer, cited = CitationService.finalize(
            "有效[[cite:2]]，无效[[cite:9]]", sources
        )

        self.assertEqual(answer, "有效[[cite:2]]，无效")
        self.assertEqual([source["citation_id"] for source in cited], [2])

    def test_finalize_returns_no_sources_without_citations(self):
        answer, cited = CitationService.finalize(
            "没有引用的回答",
            [{"citation_id": 1, "source": "一.pdf", "preview": "一"}],
        )

        self.assertEqual(answer, "没有引用的回答")
        self.assertEqual(cited, [])
