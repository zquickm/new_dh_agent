from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from services.synonym_service import replace_synonym_matcher
from config.settings import DictionaryRefreshSettings, get_settings
from services.chat_service import chat_service
from services.dictionary_service import (
    SENSITIVE_WORD,
    SYNONYM,
    DictionaryStore,
    DictionarySyncResult,
    dictionary_store,
)


logger = logging.getLogger(__name__)


class DictionaryRuntimeUpdateError(RuntimeError):
    """A downloaded snapshot could not be installed into the running process."""


class DictionaryRefreshService:
    def __init__(
        self,
        store: DictionaryStore,
        settings: DictionaryRefreshSettings,
        synonym_updater: Callable[[list[dict[str, Any]]], None],
        sensitive_updater: Callable[[list[dict[str, Any]]], None],
        *,
        sensitive_enabled: bool = True,
    ) -> None:
        self._store = store
        self._settings = settings
        self._synonym_updater = synonym_updater
        self._sensitive_updater = sensitive_updater
        self._sensitive_enabled = sensitive_enabled
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._started = False
        self._applied_versions: dict[str, int] = {}
        self._last_success_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    async def start(self) -> None:
        if self._started:
            return
        # Bind the event to the serving loop, not the module-import loop (3.9).
        self._stop_event = asyncio.Event()
        await self.refresh_once()
        self._started = True
        if self._settings.enabled:
            self._task = asyncio.create_task(
                self._refresh_loop(), name="dictionary-refresh"
            )
        logger.info(
            "dictionary_refresh_started enabled=%s interval_seconds=%s "
            "jitter_seconds=%s applied_versions=%s",
            self._settings.enabled,
            self._settings.interval_seconds,
            self._settings.jitter_seconds,
            self._applied_versions,
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            self._started = False
            return
        self._stop_event.set()
        try:
            await task
        finally:
            self._task = None
            self._started = False
        logger.info("dictionary_refresh_stopped")

    async def refresh_once(self) -> DictionarySyncResult:
        result = await self._store.synchronize()
        await self._apply_current_versions(result.versions)
        if result.used_local_cache:
            self._last_error = "REMOTE_SYNC_FAILED"
        else:
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error = None
        if result.changed_types:
            logger.info(
                "dictionary_runtime_updated changed_types=%s versions=%s",
                sorted(result.changed_types),
                self._applied_versions,
            )
        return result

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self._settings.enabled,
            "appliedVersions": dict(self._applied_versions),
            "lastSuccessAt": (
                self._last_success_at.isoformat()
                if self._last_success_at is not None
                else None
            ),
            "lastError": self._last_error,
        }

    async def _apply_current_versions(self, versions: dict[str, int]) -> None:
        errors: list[tuple[str, Exception]] = []
        if self._applied_versions.get(SYNONYM) != versions.get(SYNONYM):
            try:
                await asyncio.to_thread(
                    self._synonym_updater, self._store.synonym_groups()
                )
                self._applied_versions[SYNONYM] = versions[SYNONYM]
            except Exception as exc:
                errors.append((SYNONYM, exc))
                logger.exception("dictionary_runtime_update_failed type=%s", SYNONYM)

        if not self._sensitive_enabled:
            if SENSITIVE_WORD in versions:
                self._applied_versions[SENSITIVE_WORD] = versions[SENSITIVE_WORD]
        elif self._applied_versions.get(SENSITIVE_WORD) != versions.get(
            SENSITIVE_WORD
        ):
            try:
                await asyncio.to_thread(
                    self._sensitive_updater, self._store.sensitive_rules()
                )
                self._applied_versions[SENSITIVE_WORD] = versions[SENSITIVE_WORD]
            except Exception as exc:
                errors.append((SENSITIVE_WORD, exc))
                logger.exception(
                    "dictionary_runtime_update_failed type=%s", SENSITIVE_WORD
                )

        if errors:
            failed_types = ", ".join(item[0] for item in errors)
            raise DictionaryRuntimeUpdateError(
                f"词库运行时更新失败：{failed_types}"
            ) from errors[0][1]

    async def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            delay = self._settings.interval_seconds + random.uniform(
                0, self._settings.jitter_seconds
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = type(exc).__name__
                logger.exception(
                    "dictionary_periodic_refresh_failed current_versions=%s",
                    self._applied_versions,
                )


_settings = get_settings()
dictionary_refresh_service = DictionaryRefreshService(
    dictionary_store,
    _settings.dictionaries.refresh,
    replace_synonym_matcher,
    chat_service.replace_sensitive_word_matcher,
    sensitive_enabled=_settings.sensitive_words.enabled,
)
