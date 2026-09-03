from __future__ import annotations

import csv
import logging
import unicodedata
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


class SensitiveWordConfigurationError(RuntimeError):
    """The sensitive-word dictionary cannot be loaded safely."""


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


class SensitiveWordMatcher:
    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path.resolve()
        self._words: frozenset[str] = frozenset()
        self._signature: Optional[tuple[int, int]] = None
        self._failed_signature: Optional[tuple[int, int]] = None
        self._load_initial()

    def matches(self, text: str) -> bool:
        self._reload_if_changed()
        normalized = _normalize(text)
        return bool(normalized) and any(word in normalized for word in self._words)

    def _load_initial(self) -> None:
        signature = self._stat_signature()
        self._words = self._read_words()
        self._signature = signature

    def _reload_if_changed(self) -> None:
        try:
            signature = self._stat_signature()
        except SensitiveWordConfigurationError:
            logger.error(
                "sensitive_word_reload_failed file=%s reason=file_unavailable",
                self._file_path,
            )
            return
        if signature in {self._signature, self._failed_signature}:
            return
        try:
            words = self._read_words()
        except SensitiveWordConfigurationError:
            self._failed_signature = signature
            logger.error(
                "sensitive_word_reload_failed file=%s reason=invalid_csv",
                self._file_path,
            )
            return
        self._words = words
        self._signature = signature
        self._failed_signature = None
        logger.info(
            "sensitive_word_dictionary_reloaded file=%s word_count=%s",
            self._file_path,
            len(words),
        )

    def _stat_signature(self) -> tuple[int, int]:
        try:
            stat = self._file_path.stat()
        except OSError as exc:
            raise SensitiveWordConfigurationError(
                f"Sensitive-word CSV is unavailable: {self._file_path}"
            ) from exc
        return stat.st_mtime_ns, stat.st_size

    def _read_words(self) -> frozenset[str]:
        try:
            with self._file_path.open(
                "r",
                encoding="utf-8-sig",
                newline="",
            ) as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames is None or "word" not in reader.fieldnames:
                    raise SensitiveWordConfigurationError(
                        "Sensitive-word CSV must contain a 'word' header"
                    )
                words = frozenset(
                    normalized
                    for row in reader
                    if (normalized := _normalize(str(row.get("word") or "")))
                )
        except SensitiveWordConfigurationError:
            raise
        except (OSError, UnicodeError, csv.Error) as exc:
            raise SensitiveWordConfigurationError(
                f"Unable to read sensitive-word CSV: {self._file_path}"
            ) from exc
        if not words:
            raise SensitiveWordConfigurationError(
                "Sensitive-word CSV must contain at least one non-empty word"
            )
        return words
