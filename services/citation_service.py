import re
import unicodedata
from typing import Any


_CITATION_PATTERN = re.compile(r"\[\[cite:(\d+)\]\]")


def _article_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip()
    value = re.sub(r"\.(?:pdf|docx?|txt|xlsx?|pptx?)$", "", value, flags=re.IGNORECASE)
    return re.sub(r"[\s《》*]", "", value).casefold()


def _article_identity(source: dict[str, Any]) -> tuple[str, str]:
    return (
        str(source.get("knowledge_id") or ""),
        str(source.get("url") or source.get("source") or ""),
    )


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
        # Match complete article names only, never chunk headings or topic keywords.
        named_articles = {_article_name(name) for name in re.findall(r"《([^《》]+)》", answer)}
        plain_answer = _article_name(re.sub(r"《[^《》]+》", "", answer))
        selected_articles = {
            _article_identity(source) for source in sources
            if int(source["citation_id"]) in cited_ids
        }
        for source in sources:
            if source.get("source_type") == "database":
                continue
            name = _article_name(str(source.get("source") or ""))
            identity = _article_identity(source)
            if identity in selected_articles or not name:
                continue
            if name in named_articles or (len(name) >= 6 and name in plain_answer):
                cited_ids.add(int(source["citation_id"]))
                selected_articles.add(identity)
        return answer, [
            source
            for source in sources
            if int(source["citation_id"]) in cited_ids
        ]
