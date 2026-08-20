from __future__ import annotations

import re
from collections.abc import AsyncIterator
from textwrap import dedent

from agents.citations import CitationRegistry
from agents.llm_client import HelloAgentsLLM


_CITATION_PATTERN = re.compile(r"\[\[cite:(\d+)\]\]", re.IGNORECASE)
_SOURCE_PREVIEW_LIMIT = 1200


FINAL_ANSWER_PROMPT = dedent(
    """
    你负责根据用户问题、候选回答和可用来源目录，直接生成面向用户的最终中文回答。

    只能使用候选回答和来源目录中已有的事实，不得补造事实、数字或来源。
    保留候选回答中有用的信息，删除工具调用、检索过程和内部处理过程的描述。

    引用来源目录中的事实时，只能在对应事实后原样输出该来源的 <<SRC_数字>>。
    不得直接输出 [[cite:数字]]，不得编造或修改来源键。
    没有来源支持的事实不得添加来源键。
    """
).strip()


class AnswerMerger:
    """Generate the displayed final answer as a validated citation stream."""

    def __init__(self, llm: HelloAgentsLLM) -> None:
        self.llm = llm

    async def stream_merge(
        self,
        question: str,
        draft_answer: str,
        citation_registry: CitationRegistry,
    ) -> AsyncIterator[str]:
        messages = self._messages(
            question,
            self._validated_draft(draft_answer, citation_registry),
            self._source_catalog(citation_registry),
        )
        pending = ""
        async for content, tool_calls in self.llm.stream(messages, None):
            if tool_calls:
                raise RuntimeError("最终回答生成器不得调用工具")
            pending += content
            transformed, pending = citation_registry.transform_stream_buffer(
                pending,
                final=False,
            )
            if transformed:
                yield transformed

        transformed, pending = citation_registry.transform_stream_buffer(
            pending,
            final=True,
        )
        if pending:
            raise ValueError("最终模型输出了不完整的来源键")
        if transformed:
            yield transformed

    @staticmethod
    def _messages(
        question: str,
        draft_answer: str,
        source_catalog: str,
    ) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": FINAL_ANSWER_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户问题：\n{question}\n\n"
                    f"候选回答：\n{draft_answer}\n\n"
                    f"可用来源目录：\n{source_catalog or '（无）'}"
                ),
            },
        ]

    @staticmethod
    def _validated_draft(
        draft_answer: str,
        citation_registry: CitationRegistry,
    ) -> str:
        answer = citation_registry.transform_answer(draft_answer)
        return _CITATION_PATTERN.sub(
            lambda match: f"<<SRC_{match.group(1)}>>",
            answer,
        )

    @staticmethod
    def _source_catalog(citation_registry: CitationRegistry) -> str:
        blocks: list[str] = []
        for source in citation_registry.sources():
            citation_id = int(source["citation_id"])
            label = source.get("title") or source["source"]
            page_index = source.get("page_index")
            line = source.get("line")
            page = (
                f"第 {int(page_index) + 1} 页"
                if page_index is not None
                else (f"第 {line} 行" if line is not None else "无页码")
            )
            preview = str(source.get("preview") or "")[:_SOURCE_PREVIEW_LIMIT]
            preview = (
                preview.replace("<<SRC_", "<SRC_")
                .replace("<<IMG_", "<IMG_")
                .replace("[[cite:", "[cite:")
            )
            blocks.append(
                f"来源键：<<SRC_{citation_id}>>\n"
                f"资料：{label}\n"
                f"位置：{page}\n"
                f"证据摘要：{preview}"
            )
        return "\n\n".join(blocks)


__all__ = ["AnswerMerger"]
