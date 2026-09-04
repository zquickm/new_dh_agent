from __future__ import annotations

import csv
import io
from typing import Any

from common.sensitive_errors import SensitiveWordError
from common.text_normalization import normalize_text


class SensitiveWordAdminService:
    """Coordinate committed administrative writes and cache publication."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    def _require_repository(self):
        if self.runtime.repository is None or self.runtime.cache is None:
            raise SensitiveWordError("敏感词管理需要 MySQL/Redis 后端", status_code=503)
        return self.runtime.repository

    async def list_categories(self, scope: str):
        return await self._require_repository().list_categories(scope)

    async def list_words(self, scope: str, **filters):
        return await self._require_repository().list_words(scope, **filters)

    async def mutate(self, scope: str, operation: str, *args):
        repository = self._require_repository()
        result = await getattr(repository, operation)(scope, *args)
        await self._refresh_committed(scope, result.get("version"))
        return result

    async def _refresh_committed(self, scope: str, version):
        try:
            await self.runtime.cache.refresh(scope)
        except Exception as error:
            raise SensitiveWordError(
                "数据已保存，但缓存刷新失败；请重试刷新",
                status_code=503,
                data={"persisted": True, "refreshed": False, "version": version},
            ) from error

    async def refresh(self, scope: str):
        self._require_repository()
        try:
            snapshot = await self.runtime.cache.refresh(scope)
        except Exception as error:
            raise SensitiveWordError("敏感词缓存刷新失败", status_code=503) from error
        return {"version": snapshot["version"], "count": len(snapshot["words"])}

    async def check(self, scope: str, text: str):
        self._require_repository()
        return await self.runtime.check(text, scope=scope)

    async def import_words(self, scope: str, category_id: int, csv_text: str, *, dry_run: bool = True):
        repository = self._require_repository()
        words, duplicates, invalid = self._parse_csv(csv_text)
        if invalid:
            raise SensitiveWordError(
                "CSV 含有无效行，未写入任何词条",
                status_code=400,
                data={"invalid_lines": invalid, "valid_words": len(words), "skipped_words": duplicates, "dry_run": dry_run},
            )
        result = await repository.import_words(scope, category_id, words, dry_run=dry_run)
        result = {**result, "skipped_words": result.get("skipped_words", 0) + duplicates, "invalid_lines": []}
        if not dry_run:
            await self._refresh_committed(scope, result.get("version"))
        return result

    @staticmethod
    def _parse_csv(csv_text: str):
        reader = csv.reader(io.StringIO(csv_text.lstrip("\ufeff")), strict=True)
        words, invalid, seen = [], [], set()
        duplicates = 0
        try:
            header = next(reader, None)
            if header is None:
                return words, duplicates, [{"line": 1, "reason": "缺少 word 列"}]
            header = [field.strip() for field in header]
            if header.count("word") != 1 or len(set(header)) != len(header):
                return words, duplicates, [{"line": 1, "reason": "CSV 必须包含唯一的 word 列"}]
            word_index = header.index("word")
            while True:
                line = reader.line_num + 1
                row = next(reader, None)
                if row is None:
                    break
                if not row:
                    continue
                if len(row) != len(header):
                    invalid.append({"line": line, "reason": "列数与表头不一致"})
                    continue
                word = row[word_index].strip()
                if not word or len(word) > 255:
                    invalid.append({"line": line, "reason": "word 必须为 1 到 255 个字符"})
                    continue
                normalized = normalize_text(word)
                if normalized in seen:
                    duplicates += 1
                    continue
                seen.add(normalized)
                words.append(word)
        except csv.Error:
            invalid.append({"line": reader.line_num, "reason": "CSV 格式错误"})
        return words, duplicates, invalid
