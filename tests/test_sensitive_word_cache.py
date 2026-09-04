from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock


class SensitiveWordCacheTest(unittest.IsolatedAsyncioTestCase):
    def cache(self, repository=None, redis=None):
        from services.sensitive_word_cache import SensitiveWordCache
        self.repo = repository or type('Repository', (), {'load_snapshot': AsyncMock(return_value={'scope': 'agent', 'version': 1, 'words': ['blocked']})})()
        self.redis = redis or type('Redis', (), {'get': AsyncMock(return_value=None), 'eval': AsyncMock(return_value=1)})()
        return SensitiveWordCache(self.repo, self.redis)

    async def test_cold_start_uses_database_when_redis_is_down(self):
        cache = self.cache()
        self.redis.get.side_effect = ConnectionError('secret-password')
        self.redis.eval.side_effect = ConnectionError('secret-password')
        self.assertEqual((await cache.get_snapshot('agent'))['words'], ['blocked'])

    async def test_worker_observes_published_update_and_retains_it_on_transport_error(self):
        cache = self.cache()
        await cache.get_snapshot('agent')
        self.redis.get.return_value = json.dumps({'scope': 'agent', 'version': 2, 'words': ['new']})
        self.assertEqual((await cache.get_snapshot('agent'))['words'], ['new'])
        self.redis.get.side_effect = ConnectionError('secret')
        self.repo.load_snapshot.side_effect = ConnectionError('database secret')
        self.assertEqual((await cache.get_snapshot('agent'))['words'], ['new'])

    async def test_corrupt_or_wrong_scope_cache_recovers_database_and_stale_cache_does_not_regress(self):
        cache = self.cache()
        for value in ('invalid', '{}', '{"scope":"other","version":4,"words":[]}', '{"scope":"agent","version":999,"words":["UPPER"]}'):
            self.redis.get.return_value = value
            self.assertEqual((await cache.get_snapshot('agent'))['version'], 1)
        self.redis.get.return_value = json.dumps({'scope': 'agent', 'version': 0, 'words': []})
        self.assertEqual((await cache.get_snapshot('agent'))['version'], 1)

    async def test_refresh_publish_failure_is_sanitized_and_does_not_destroy_lkg(self):
        from common.sensitive_errors import SensitiveWordError
        cache = self.cache()
        await cache.get_snapshot('agent')
        self.redis.eval.side_effect = ConnectionError('password=secret')
        with self.assertRaises(SensitiveWordError) as raised:
            await cache.refresh('agent')
        self.assertNotIn('secret', str(raised.exception))
        self.redis.get.side_effect = ConnectionError('secret')
        self.assertEqual((await cache.get_snapshot('agent'))['words'], ['blocked'])

    async def test_no_usable_source_raises_instead_of_empty_allow_list(self):
        from common.sensitive_errors import SensitiveWordError
        cache = self.cache()
        self.redis.get.side_effect = ConnectionError('secret')
        self.repo.load_snapshot.side_effect = ConnectionError('secret')
        with self.assertRaises(SensitiveWordError):
            await cache.get_snapshot('agent')


@unittest.skipUnless(os.environ.get('SENSITIVE_TEST_REDIS_URL'), 'requires isolated Redis URL')
class SensitiveWordRedisIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_corrupt_high_version_snapshot_can_be_repaired(self):
        import redis.asyncio as redis
        from services.sensitive_word_cache import SensitiveWordCache
        import uuid
        client = redis.from_url(os.environ['SENSITIVE_TEST_REDIS_URL'])
        prefix = 'test:sensitive:' + uuid.uuid4().hex
        repo = type('Repository', (), {'load_snapshot': AsyncMock(return_value={'scope':'agent','version':2,'words':['valid']})})()
        cache = SensitiveWordCache(repo, client, key_prefix=prefix)
        try:
            await client.set(prefix + ':agent', json.dumps({'scope':'agent','version':999,'words':['INVALID']}))
            self.assertEqual((await cache.refresh('agent'))['words'], ['valid'])
            self.assertEqual(json.loads(await client.get(prefix + ':agent'))['version'], 2)
        finally:
            await client.delete(prefix + ':agent')
            await client.aclose()

    async def test_atomic_publish_prevents_old_snapshot_overwriting_new(self):
        import redis.asyncio as redis
        from services.sensitive_word_cache import SensitiveWordCache
        import uuid
        client = redis.from_url(os.environ['SENSITIVE_TEST_REDIS_URL'])
        prefix = 'test:sensitive:' + uuid.uuid4().hex
        repo = type('Repository', (), {'load_snapshot': AsyncMock(return_value={'scope':'agent','version':2,'words':['new']})})()
        cache = SensitiveWordCache(repo, client, key_prefix=prefix)
        try:
            await cache.refresh('agent')
            repo.load_snapshot.return_value = {'scope':'agent','version':1,'words':['old']}
            await cache.refresh('agent')
            second_worker = SensitiveWordCache(repo, client, key_prefix=prefix)
            self.assertEqual((await second_worker.get_snapshot('agent'))['words'], ['new'])
        finally:
            keys = await client.keys(prefix + '*')
            if keys:
                await client.delete(*keys)
            await client.aclose()
