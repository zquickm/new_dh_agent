from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiohttp

from config.settings import DictionarySettings, InternalRequestSettings, get_settings


logger = logging.getLogger(__name__)

SYNONYM = "SYNONYM"
SENSITIVE_WORD = "SENSITIVE_WORD"
DICTIONARY_TYPES = (SYNONYM, SENSITIVE_WORD)


class DictionaryConfigurationError(RuntimeError):
    """A published dictionary snapshot cannot be loaded safely."""


@dataclass(frozen=True)
class DictionarySyncResult:
    changed_types: frozenset[str]
    versions: dict[str, int]
    used_local_cache: bool = False


class DictionaryStore:
    """Synchronize immutable dh-knowledge snapshots into a persistent local cache."""

    def __init__(
        self,
        settings: DictionarySettings,
        internal_request: InternalRequestSettings,
    ) -> None:
        self._settings = settings
        self._cache_directory = settings.resolved_cache_directory()
        self._header_name = internal_request.header_name
        self._token = internal_request.token.get_secret_value()
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def synchronize(self) -> DictionarySyncResult:
        """Load local snapshots, then replace them when dh-knowledge has newer ones."""
        async with self._lock:
            self._cache_directory.mkdir(parents=True, exist_ok=True)
            self._load_local_snapshots()
            try:
                changed_types = await self._synchronize_remote()
            except Exception as exc:
                missing = [item for item in DICTIONARY_TYPES if item not in self._snapshots]
                if missing:
                    raise DictionaryConfigurationError(
                        "dh-knowledge不可用，且本地缺少词库快照：" + ", ".join(missing)
                    ) from exc
                logger.warning(
                    "dictionary_sync_failed_using_local_cache reason=%s versions=%s",
                    type(exc).__name__,
                    self.versions(),
                )
                return DictionarySyncResult(
                    frozenset(), self.versions(), used_local_cache=True
                )
            return DictionarySyncResult(changed_types, self.versions())

    def synonym_groups(self) -> list[dict[str, Any]]:
        return self._data_list(SYNONYM, "groups")

    def sensitive_rules(self) -> list[dict[str, Any]]:
        return self._data_list(SENSITIVE_WORD, "rules")

    def versions(self) -> dict[str, int]:
        return {
            item: int(snapshot["version"])
            for item, snapshot in self._snapshots.items()
        }

    async def _synchronize_remote(self) -> frozenset[str]:
        timeout = aiohttp.ClientTimeout(
            total=self._settings.timeout,
            connect=self._settings.connect_timeout,
        )
        connector = aiohttp.TCPConnector(ssl=self._settings.verify_ssl)
        headers = {self._header_name: self._token}
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector, headers=headers
        ) as session:
            manifest = await self._get_data(session, "/internal/v1/dictionaries/manifest")
            if not isinstance(manifest, list):
                raise DictionaryConfigurationError("dh-knowledge manifest格式错误")
            remote = {
                str(item.get("dictionaryType")): item
                for item in manifest
                if isinstance(item, dict)
            }
            updates: dict[str, dict[str, Any]] = {}
            for dictionary_type in DICTIONARY_TYPES:
                item = remote.get(dictionary_type)
                if item is None:
                    raise DictionaryConfigurationError(
                        f"dh-knowledge尚未发布{dictionary_type}词库"
                    )
                remote_version = self._positive_int(item.get("version"), "version")
                remote_checksum = str(item.get("checksum") or "")
                local = self._snapshots.get(dictionary_type)
                if local is not None:
                    local_version = int(local["version"])
                    if local_version > remote_version:
                        logger.warning(
                            "dictionary_remote_version_behind type=%s local=%s remote=%s",
                            dictionary_type,
                            local_version,
                            remote_version,
                        )
                        continue
                    if local_version == remote_version:
                        if str(local["checksum"]) != remote_checksum:
                            raise DictionaryConfigurationError(
                                f"{dictionary_type}同版本校验值发生变化"
                            )
                        continue
                snapshot = await self._get_data(
                    session,
                    f"/internal/v1/dictionaries/{dictionary_type}/versions/{remote_version}",
                )
                validated = self._validate_snapshot(snapshot, dictionary_type)
                if validated["checksum"] != remote_checksum:
                    raise DictionaryConfigurationError(
                        f"{dictionary_type} manifest与快照校验值不一致"
                    )
                updates[dictionary_type] = validated

            for dictionary_type, validated in updates.items():
                self._persist(validated)
                self._snapshots[dictionary_type] = validated
                logger.info(
                    "dictionary_updated type=%s version=%s item_count=%s",
                    dictionary_type,
                    validated["version"],
                    validated["itemCount"],
                )
            return frozenset(updates)

    async def _get_data(
        self, session: aiohttp.ClientSession, path: str
    ) -> Any:
        url = f"{self._settings.base_url.rstrip('/')}/{path.lstrip('/')}"
        async with session.get(url) as response:
            if response.status != 200:
                body = (await response.text())[:500]
                raise DictionaryConfigurationError(
                    f"dh-knowledge请求失败 status={response.status} body={body}"
                )
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise DictionaryConfigurationError("dh-knowledge响应格式错误")
        return payload.get("data")

    def _load_local_snapshots(self) -> None:
        for dictionary_type in DICTIONARY_TYPES:
            path = self._snapshot_path(dictionary_type)
            if not path.exists():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._snapshots[dictionary_type] = self._validate_snapshot(
                    payload, dictionary_type
                )
            except (OSError, UnicodeError, json.JSONDecodeError, DictionaryConfigurationError):
                logger.exception("dictionary_local_cache_invalid file=%s", path)

    def _persist(self, snapshot: dict[str, Any]) -> None:
        path = self._snapshot_path(str(snapshot["dictionaryType"]))
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_snapshot(
        self, payload: Any, expected_type: str
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DictionaryConfigurationError(f"{expected_type}快照格式错误")
        dictionary_type = str(payload.get("dictionaryType") or "")
        if dictionary_type != expected_type:
            raise DictionaryConfigurationError(f"{expected_type}快照类型错误")
        version = self._positive_int(payload.get("version"), "version")
        checksum = str(payload.get("checksum") or "")
        if len(checksum) != 64 or any(
            character not in "0123456789abcdefABCDEF" for character in checksum
        ):
            raise DictionaryConfigurationError(f"{expected_type}快照校验值错误")
        data = payload.get("data")
        list_key = "groups" if expected_type == SYNONYM else "rules"
        if not isinstance(data, dict) or not isinstance(data.get(list_key), list):
            raise DictionaryConfigurationError(f"{expected_type}快照数据格式错误")
        item_count = payload.get("itemCount")
        if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 0:
            raise DictionaryConfigurationError(f"{expected_type}快照条目数错误")
        return {
            "dictionaryType": dictionary_type,
            "version": version,
            "checksum": checksum,
            "itemCount": item_count,
            "publishedAt": payload.get("publishedAt"),
            "data": data,
        }

    def _data_list(self, dictionary_type: str, key: str) -> list[dict[str, Any]]:
        snapshot = self._snapshots.get(dictionary_type)
        if snapshot is None:
            raise DictionaryConfigurationError(f"{dictionary_type}词库尚未初始化")
        return [item for item in snapshot["data"][key] if isinstance(item, dict)]

    def _snapshot_path(self, dictionary_type: str) -> Path:
        return self._cache_directory / f"{dictionary_type.lower()}.json"

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise DictionaryConfigurationError(f"词库{name}格式错误")
        return value


_settings = get_settings()
dictionary_store = DictionaryStore(_settings.dictionaries, _settings.internal_request)
