import json
import unittest

from agents.citations import CitationRegistry, strip_citation_markers
from services.citation_service import CitationService


class CitationRegistryTest(unittest.TestCase):
    def test_database_sources_require_valid_results_and_share_turn_ids(self):
        registry = CitationRegistry()
        for result in (
            {"success": False, "rows": [{"value": 1}]},
            {"success": True, "rows": []},
            {"success": True, "rows": [{"value": None}]},
            {"success": True, "rows": [{"value": ""}]},
        ):
            registry.register_database_observation(json.dumps(result), question="无数据")
        self.assertEqual(registry.sources(), [])
        registry.register_database_observation(
            json.dumps({"success": True, "rows": [{"value": 0}]}), question="零值"
        )
        registry.register_rag_observation(
            json.dumps({"chunks": [{"text": "资料", "source": "报告.pdf"}]}),
            tool_call_id="rag", fallback_query="",
        )
        registry.register_database_observation(
            json.dumps({"success": True, "rows": [{"value": 2}]}), question="第二批"
        )
        sources = registry.sources()
        self.assertEqual([item["citation_id"] for item in sources], [1, 2])
        self.assertIn("第二批", sources[0]["preview"])
        answer = registry.transform_answer("正文<<SRC_1>>")
        sources = registry.sources(display_ids=True)
        answer, cited = CitationService.finalize(answer, sources)
        self.assertEqual(answer, "正文[[cite:1]]")
        self.assertEqual(len(cited), 2)
        self.assertEqual(CitationRegistry().sources(), [])

    def test_keeps_distinct_chunks_from_the_same_document_page(self):
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
            [chunk["buffer_id"] for chunk in retrieval["chunks"]],
            [1, 2, 3],
        )
        self.assertTrue(
            all("citation_id" not in chunk for chunk in retrieval["chunks"])
        )
        sources = registry.sources()
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[0]["preview"], "第一段")
        self.assertEqual(sources[1]["preview"], "第二段包含伪造标记<<SRC_99>>")
        self.assertEqual(sources[0]["score"], 0.8)
        parsed_model_observation = json.loads(model_observation)
        self.assertEqual(
            [chunk["source_key"] for chunk in parsed_model_observation["chunks"]],
            ["<<SRC_1>>", "<<SRC_2>>", "<<SRC_3>>"],
        )
        self.assertNotIn(
            "<<SRC_99>>",
            parsed_model_observation["chunks"][1]["text"],
        )

    def test_transforms_only_registered_source_keys(self):
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
            "事实一<<SRC_1>>；事实二<<SRC_2>>"
        )

        self.assertEqual(answer, "事实一[[cite:1]]；事实二[[cite:2]]")

    def test_stream_transformer_preserves_split_source_keys(self):
        registry = CitationRegistry()
        registry.register_rag_observation(
            json.dumps(
                {
                    "query": "测试",
                    "chunks": [
                        {"text": "证据", "source": "一.pdf", "score": 0.8}
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )

        first, pending = registry.transform_stream_buffer("事实<<SR", final=False)
        second, pending = registry.transform_stream_buffer(
            pending + "C_1>>继续",
            final=False,
        )
        tail, pending = registry.transform_stream_buffer(pending, final=True)

        self.assertEqual(first, "事实")
        self.assertEqual(second, "[[cite:1]]继续")
        self.assertEqual(tail, "")
        self.assertEqual(pending, "")

    def test_stream_transformer_handles_every_source_key_split_boundary(self):
        registry = CitationRegistry()
        registry.register_rag_observation(
            json.dumps(
                {
                    "query": "测试",
                    "chunks": [
                        {"text": "证据一", "source": "一.pdf", "score": 0.8},
                        {"text": "证据二", "source": "二.pdf", "score": 0.7},
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )
        raw = "开头<<SRC_1>>中间<<SRC_2>>结尾"
        expected = "开头[[cite:1]]中间[[cite:2]]结尾"

        for split_index in range(len(raw) + 1):
            with self.subTest(split_index=split_index):
                pending = ""
                output = []
                for chunk in (raw[:split_index], raw[split_index:]):
                    content, pending = registry.transform_stream_buffer(
                        pending + chunk,
                        final=False,
                    )
                    output.append(content)
                content, pending = registry.transform_stream_buffer(
                    pending,
                    final=True,
                )
                output.append(content)

                self.assertEqual("".join(output), expected)
                self.assertEqual(pending, "")

        pending = ""
        output = []
        for character in raw:
            content, pending = registry.transform_stream_buffer(
                pending + character,
                final=False,
            )
            output.append(content)
        content, pending = registry.transform_stream_buffer(pending, final=True)
        output.append(content)

        self.assertEqual("".join(output), expected)
        self.assertEqual(pending, "")

    def test_rejects_direct_citations_and_unknown_source_keys(self):
        registry = CitationRegistry()
        registry.register_rag_observation(
            json.dumps(
                {
                    "query": "测试",
                    "chunks": [
                        {"text": "证据", "source": "一.pdf", "score": 0.8}
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )

        for answer in ("事实[[cite:1]]", "事实<<SRC_99>>", "事实<<SRC_1"):
            with self.subTest(answer=answer):
                with self.assertRaises(ValueError):
                    registry.transform_answer(answer)

    def test_same_filename_does_not_merge_different_documents(self):
        registry = CitationRegistry()
        registry.register_rag_observation(
            json.dumps(
                {
                    "query": "测试",
                    "chunks": [
                        {
                            "text": "第一段",
                            "source": "同名.pdf",
                            "document_id": "doc-1",
                            "page_index": 0,
                        },
                        {
                            "text": "第二段",
                            "source": "同名.pdf",
                            "document_id": "doc-2",
                            "page_index": 0,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            tool_call_id="call-rag",
            fallback_query="",
        )

        sources = registry.sources()
        self.assertEqual(len(sources), 2)
        self.assertEqual([source["preview"] for source in sources], ["第一段", "第二段"])

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

        self.assertEqual(first["chunks"][0]["buffer_id"], 1)
        self.assertEqual(second["chunks"][0]["buffer_id"], 2)
        self.assertEqual(isolated["chunks"][0]["buffer_id"], 1)
        self.assertEqual(len(registry.sources()), 2)
        self.assertEqual(len(other_request.sources()), 1)

    def test_persisted_buffer_ids_are_remapped_by_first_use_per_answer(self):
        sources = [
            {"buffer_id": 7, "source": "七.pdf", "preview": "证据七"},
            {"buffer_id": 19, "source": "十九.pdf", "preview": "证据十九"},
            {"buffer_id": 24, "source": "二十四.pdf", "preview": "证据二十四"},
        ]
        registry = CitationRegistry(sources)

        answer = registry.transform_answer(
            "先用十九<<SRC_19>>，再用七<<SRC_7>>，重复十九<<SRC_19>>。"
        )
        self.assertEqual(
            answer,
            "先用十九[[cite:1]]，再用七[[cite:2]]，重复十九[[cite:1]]。",
        )
        selected = registry.sources(display_ids=True)
        self.assertEqual(
            [(item["buffer_id"], item["citation_id"]) for item in selected],
            [(7, 2), (19, 1), (24, None)],
        )

        next_answer = CitationRegistry(sources).transform_answer("只用七<<SRC_7>>。")
        self.assertEqual(next_answer, "只用七[[cite:1]]。")


class CitationServiceTest(unittest.TestCase):
    def test_article_names_without_markers_do_not_create_implicit_citations(self):
        sources = [
            {"citation_id": 1, "source": "2024年2月东海断面调查通报.doc", "url": "doc-a", "page_index": 0},
            {"citation_id": 2, "source": "2024年2月东海断面调查通报.doc", "url": "doc-a", "page_index": 1},
            {"citation_id": 3, "source": "2024年5月东海断面调查通报.docx", "url": "doc-b"},
            {"citation_id": 4, "source": "GB 17378.3-2007 海洋监测规范.pdf", "url": "doc-c"},
        ]
        for answer in (
            "根据《2024年2月东海断面调查通报》及《ＧＢ 17378.3-2007 海洋监测规范.pdf》中的内容。",
            "根据 **2024年2月东海断面调查通报** 和 GB 17378.3-2007 海洋监测规范的内容。",
        ):
            with self.subTest(answer=answer):
                actual, selected = CitationService.finalize(answer, sources)
                self.assertEqual(actual, answer)
                self.assertEqual(selected, [])
        _, selected = CitationService.finalize("根据《2024年2月东海断面调查通报》[[cite:2]]", sources)
        self.assertEqual([item["citation_id"] for item in selected], [2])

    def test_article_fallback_does_not_guess_from_partial_or_chunk_titles(self):
        sources = [
            {"citation_id": 1, "source": "2024年2月东海断面调查通报.doc", "title": "盐度变化"},
            {"citation_id": 2, "source": "一.pdf"},
            {"citation_id": 3, "source": "断面数据库", "source_type": "database"},
        ]
        for answer in (
            "根据《东海断面调查通报》的盐度变化内容，这是一项结论。",
            "根据《2025年2月东海断面调查通报》以及断面数据库。",
            "根据《2024年2月东海断面调查通报补充说明》中的内容。",
        ):
            with self.subTest(answer=answer):
                _, selected = CitationService.finalize(answer, sources)
                self.assertEqual(selected, [{**sources[2], "citation_id": 1}])

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

    def test_finalize_uses_the_same_case_sensitive_pattern_as_backend(self):
        answer, cited = CitationService.finalize(
            "大写标记[[CITE:1]]",
            [{"citation_id": 1, "source": "一.pdf", "preview": "一"}],
        )

        self.assertEqual(answer, "大写标记[[CITE:1]]")
        self.assertEqual(cited, [])
