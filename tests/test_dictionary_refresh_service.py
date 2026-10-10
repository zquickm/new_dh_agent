import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from agents.tools.rag_search import RagSearchTool
from config.settings import DictionaryRefreshSettings, get_settings
from services.dictionary_refresh_service import (
    DictionaryRefreshService,
    DictionaryRuntimeUpdateError,
)
from services.dictionary_service import (
    SENSITIVE_WORD,
    SYNONYM,
    DictionarySyncResult,
)
from services.synonym_service import expand_query_synonyms, replace_synonym_matcher


class FakeDictionaryStore:
    def __init__(self):
        self.versions = {SYNONYM: 1, SENSITIVE_WORD: 1}
        self.changed_types = {SYNONYM, SENSITIVE_WORD}
        self.used_local_cache = False
        self.calls = 0

    async def synchronize(self):
        self.calls += 1
        return DictionarySyncResult(
            frozenset(self.changed_types),
            dict(self.versions),
            self.used_local_cache,
        )

    def synonym_groups(self):
        return [{"standardTerm": "溶解氧", "terms": [{"term": "DO"}]}]

    def sensitive_rules(self):
        return [{"word": "敏感词"}]


class DictionaryRefreshServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_updates_shared_input_and_rag_matcher(self):
        store = FakeDictionaryStore()
        service = self.service(store, replace_synonym_matcher, lambda _rules: None)
        backend = AsyncMock()
        backend.search_all.return_value = []
        rag = RagSearchTool(backend)
        with patch("services.synonym_service._active_synonym_matcher", None):
            await service.refresh_once()
            self.assertEqual(expand_query_synonyms("DO"), "DO（溶解氧）")
            store.versions[SYNONYM] = 2
            with patch.object(store, "synonym_groups", return_value=[
                {"standardTerm": "溶解氧浓度", "terms": [{"term": "DO"}]},
            ]):
                await service.refresh_once()
            expanded = expand_query_synonyms("DO")
            self.assertEqual(expanded, "DO（溶解氧浓度）")
            await rag.execute(expanded)
            backend.search_all.assert_awaited_with(expanded, [(b.knowledge_id.strip(), b.recall_top_n, b.top_n) for b in get_settings().business_scenarios.general.rag.knowledge_bases], {"tags": []}, score=get_settings().business_scenarios.general.rag.score, result_score_threshold=get_settings().business_scenarios.general.rag.result_score_threshold)
            await rag.execute("DO")
            backend.search_all.assert_awaited_with(expanded, [(b.knowledge_id.strip(), b.recall_top_n, b.top_n) for b in get_settings().business_scenarios.general.rag.knowledge_bases], {"tags": []}, score=get_settings().business_scenarios.general.rag.score, result_score_threshold=get_settings().business_scenarios.general.rag.result_score_threshold)

    def service(self, store, synonyms, sensitive, *, enabled=False):
        return DictionaryRefreshService(
            store,
            DictionaryRefreshSettings(
                enabled=enabled,
                interval_seconds=10,
                jitter_seconds=0,
            ),
            synonyms,
            sensitive,
        )

    async def test_applies_initial_snapshots_and_only_replaces_new_version(self):
        store = FakeDictionaryStore()
        synonym_updates = []
        sensitive_updates = []
        service = self.service(
            store,
            synonym_updates.append,
            sensitive_updates.append,
        )

        await service.start()
        store.versions[SYNONYM] = 2
        store.changed_types = {SYNONYM}
        await service.refresh_once()

        self.assertEqual(len(synonym_updates), 2)
        self.assertEqual(len(sensitive_updates), 1)
        self.assertEqual(
            service.status()["appliedVersions"],
            {SYNONYM: 2, SENSITIVE_WORD: 1},
        )

    async def test_failed_runtime_install_is_retried_for_same_version(self):
        store = FakeDictionaryStore()
        attempts = 0

        def update_synonyms(_groups):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("invalid matcher")

        service = self.service(store, update_synonyms, lambda _rules: None)

        with self.assertRaises(DictionaryRuntimeUpdateError):
            await service.refresh_once()
        await service.refresh_once()

        self.assertEqual(attempts, 2)
        self.assertEqual(service.status()["appliedVersions"][SYNONYM], 1)

    async def test_remote_failure_keeps_applied_versions_and_records_status(self):
        store = FakeDictionaryStore()
        service = self.service(store, lambda _groups: None, lambda _rules: None)
        await service.start()
        store.changed_types = set()
        store.used_local_cache = True

        await service.refresh_once()

        self.assertEqual(
            service.status()["appliedVersions"],
            {SYNONYM: 1, SENSITIVE_WORD: 1},
        )
        self.assertEqual(service.status()["lastError"], "REMOTE_SYNC_FAILED")

    async def test_background_task_stops_without_waiting_for_interval(self):
        store = FakeDictionaryStore()
        service = self.service(
            store, lambda _groups: None, lambda _rules: None, enabled=True
        )

        await service.start()
        await asyncio.wait_for(service.stop(), timeout=1)

        self.assertEqual(store.calls, 1)


class RefreshLifecycleTest(unittest.TestCase):
    def test_service_created_on_another_loop_starts_and_stops(self):
        async def create():
            return DictionaryRefreshService(
                FakeDictionaryStore(),
                DictionaryRefreshSettings(interval_seconds=10, jitter_seconds=0),
                lambda _groups: None,
                lambda _rules: None,
            )

        service = asyncio.run(create())

        async def serve():
            await service.start()
            # Let the background task enter Event.wait before shutting down.
            await asyncio.sleep(0.01)
            await service.stop()

        asyncio.run(serve())


if __name__ == "__main__":
    unittest.main()
