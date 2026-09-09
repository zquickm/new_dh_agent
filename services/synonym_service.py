from __future__ import annotations

import re
from typing import Any, Optional

from services.dictionary_service import dictionary_store


SynonymMatcher = tuple[re.Pattern[str], dict[str, str], set[str]]
_active_synonym_matcher: Optional[SynonymMatcher] = None


def build_synonym_matcher(groups: list[dict[str, Any]]) -> SynonymMatcher:
    aliases: dict[str, str] = {}
    standards: set[str] = set()
    tokens: set[str] = set()
    for group in groups:
        standard = str(group.get("standardTerm") or "").strip()
        if not standard:
            continue
        standards.add(standard.casefold())
        tokens.add(standard)
        for term in group.get("terms") or []:
            if not isinstance(term, dict):
                continue
            synonym = str(term.get("term") or "").strip()
            if not synonym:
                continue
            aliases.setdefault(synonym.casefold(), standard)
            tokens.add(synonym)
    if not tokens:
        raise RuntimeError("Published synonym snapshot contains no terms")
    ordered_tokens = sorted(tokens, key=len, reverse=True)
    alternatives = []
    for token in ordered_tokens:
        alternative = re.escape(token)
        standard = aliases.get(token.casefold())
        if standard is not None and token.casefold() not in standards:
            # Consume existing annotations as a unit so a second pass cannot
            # expand aliases inside the standard term.
            escaped = re.escape(standard)
            alternative += rf"(?:（{escaped}）|\({escaped}\))?"
        alternatives.append(alternative)
    pattern = re.compile("|".join(alternatives), re.I)
    return pattern, aliases, standards


def replace_synonym_matcher(groups: list[dict[str, Any]]) -> None:
    global _active_synonym_matcher
    matcher = build_synonym_matcher(groups)
    _active_synonym_matcher = matcher


def _synonym_matcher() -> SynonymMatcher:
    global _active_synonym_matcher
    matcher = _active_synonym_matcher
    if matcher is None:
        matcher = build_synonym_matcher(dictionary_store.synonym_groups())
        _active_synonym_matcher = matcher
    return matcher


def clear_synonym_matcher_cache() -> None:
    global _active_synonym_matcher
    _active_synonym_matcher = None


def expand_query_synonyms(query: str) -> str:
    """Append standard terms to aliases in user input or tool queries."""
    pattern, aliases, standards = _synonym_matcher()

    def replace(match: re.Match[str]) -> str:
        synonym = match.group()
        key = synonym.casefold()
        standard = aliases.get(key)
        if standard is None or key in standards:
            return synonym

        start, end = match.span()
        if (
            start
            and synonym[0].isascii()
            and synonym[0].isalnum()
            and query[start - 1].isascii()
            and query[start - 1].isalnum()
        ) or (
            end < len(query)
            and synonym[-1].isascii()
            and synonym[-1].isalnum()
            and query[end].isascii()
            and query[end].isalnum()
        ):
            return synonym
        if query.startswith((f"（{standard}）", f"({standard})"), end):
            return synonym
        return f"{synonym}（{standard}）"

    return pattern.sub(replace, query)

