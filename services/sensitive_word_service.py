from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any


class SensitiveWordConfigurationError(RuntimeError):
    """The sensitive-word dictionary cannot be loaded safely."""


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


class SensitiveWordMatcher:
    def __init__(self, rules: Iterable[Mapping[str, Any]]) -> None:
        words = frozenset(
            normalized
            for rule in rules
            if (normalized := _normalize(str(rule.get("word") or "")))
        )
        if not words:
            raise SensitiveWordConfigurationError(
                "Published sensitive-word snapshot contains no rules"
            )
        self._words = words

    def matches(self, text: str) -> bool:
        normalized = _normalize(text)
        return bool(normalized) and any(word in normalized for word in self._words)
