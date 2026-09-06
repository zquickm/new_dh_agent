import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from config.settings import DictionarySettings, InternalRequestSettings
from services.dictionary_service import (
    DictionaryConfigurationError,
    DictionaryStore,
    SENSITIVE_WORD,
    SYNONYM,
)


def snapshot(dictionary_type, version, checksum_char, data, item_count=1):
    return {
        "dictionaryType": dictionary_type,
        "version": version,
        "checksum": checksum_char * 64,
        "itemCount": item_count,
        "publishedAt": "2026-09-06T08:00:00",
        "data": data,
    }


class DictionaryStoreTest(unittest.IsolatedAsyncioTestCase):
    def store(self, directory):
        return DictionaryStore(
            DictionarySettings(
                base_url="http://knowledge.test",
                cache_directory=str(directory),
            ),
            InternalRequestSettings(
                header_name="X-DH-Internal-Token",
                token="1234567890abcdef",
            ),
        )

    async def test_downloads_new_versions_and_persists_json_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            synonym = snapshot(
                SYNONYM,
                2,
                "a",
                {
                    "groups": [
                        {
                            "standardTerm": "溶解氧",
                            "terms": [{"term": "DO"}],
                        }
                    ]
                },
            )
            sensitive = snapshot(
                SENSITIVE_WORD,
                3,
                "b",
                {"rules": [{"word": "测试敏感词", "applyScope": "CHAT_INPUT"}]},
            )
            manifest = [
                {"dictionaryType": SYNONYM, "version": 2, "checksum": "a" * 64},
                {
                    "dictionaryType": SENSITIVE_WORD,
                    "version": 3,
                    "checksum": "b" * 64,
                },
            ]
            responses = AsyncMock(side_effect=[manifest, synonym, sensitive])

            with patch.object(store, "_get_data", responses):
                await store.synchronize()

            self.assertEqual(store.versions(), {SYNONYM: 2, SENSITIVE_WORD: 3})
            self.assertEqual(store.synonym_groups()[0]["standardTerm"], "溶解氧")
            self.assertEqual(store.sensitive_rules()[0]["word"], "测试敏感词")
            persisted = json.loads(
                (Path(directory) / "synonym.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["version"], 2)

    async def test_same_versions_only_fetch_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            synonym = snapshot(SYNONYM, 1, "a", {"groups": [{"terms": []}]})
            sensitive = snapshot(SENSITIVE_WORD, 1, "b", {"rules": [{}]})
            store._persist(synonym)
            store._persist(sensitive)
            manifest = [
                {"dictionaryType": SYNONYM, "version": 1, "checksum": "a" * 64},
                {
                    "dictionaryType": SENSITIVE_WORD,
                    "version": 1,
                    "checksum": "b" * 64,
                },
            ]
            responses = AsyncMock(return_value=manifest)

            with patch.object(store, "_get_data", responses):
                await store.synchronize()

            self.assertEqual(responses.await_count, 1)

    async def test_uses_last_valid_cache_when_remote_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            store._persist(snapshot(SYNONYM, 4, "a", {"groups": [{"terms": []}]}))
            store._persist(snapshot(SENSITIVE_WORD, 5, "b", {"rules": [{}]}))

            with patch.object(
                store,
                "_synchronize_remote",
                AsyncMock(side_effect=OSError("offline")),
            ):
                await store.synchronize()

            self.assertEqual(store.versions(), {SYNONYM: 4, SENSITIVE_WORD: 5})

    async def test_fails_without_remote_or_local_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(directory)
            with patch.object(
                store,
                "_synchronize_remote",
                AsyncMock(side_effect=OSError("offline")),
            ):
                with self.assertRaises(DictionaryConfigurationError):
                    await store.synchronize()


if __name__ == "__main__":
    unittest.main()
