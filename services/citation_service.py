import re
from typing import Any


_CITATION_PATTERN = re.compile(r"\[\[cite:(\d+)\]\]")


class CitationService:
    @staticmethod
    def database_footer(sources: list[dict[str, Any]]) -> str:
        markers = "".join(
            f"[[cite:{source['citation_id']}]]"
            for source in sources if source.get("source_type") == "database"
        )
        return f"\n\n数据来源：断面数据库{markers}" if markers else ""

    @staticmethod
    def finalize(
        answer: str,
        sources: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        valid_ids = {
            int(source["citation_id"])
            for source in sources
            if source.get("citation_id") is not None
        }

        def remove_invalid(match: re.Match[str]) -> str:
            return match.group(0) if int(match.group(1)) in valid_ids else ""

        answer = _CITATION_PATTERN.sub(remove_invalid, answer)
        cited_ids = {
            int(match.group(1)) for match in _CITATION_PATTERN.finditer(answer)
        }
        return answer, [
            source
            for source in sources
            if int(source["citation_id"]) in cited_ids
        ]
