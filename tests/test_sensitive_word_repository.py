from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid


class RepositoryBoundaryTest(unittest.IsolatedAsyncioTestCase):
    async def test_unsupported_scope_is_rejected_before_opening_database(self):
        from common.sensitive_errors import SensitiveWordError
        from repositories.sensitive_word_repository import SensitiveWordRepository
        repository = SensitiveWordRepository(SimpleNamespace())
        with self.assertRaises(SensitiveWordError) as raised:
            await repository.load_snapshot('external')
        self.assertEqual(raised.exception.status_code, 400)


@unittest.skipUnless(os.environ.get('SENSITIVE_TEST_MYSQL_PORT'), 'requires isolated MySQL test port')
class SensitiveWordRepositoryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncmy
        from repositories.sensitive_word_repository import SensitiveWordRepository
        self.database = 'sensitive_test_' + uuid.uuid4().hex
        self.connection = await asyncmy.connect(host='127.0.0.1', port=int(os.environ['SENSITIVE_TEST_MYSQL_PORT']), user='root', password='isolated-test-password', autocommit=True)
        async with self.connection.cursor() as cursor:
            await cursor.execute('CREATE DATABASE ' + self.database + ' CHARACTER SET utf8mb4')
            await cursor.execute('USE ' + self.database)
            migration = (Path(__file__).parents[1] / 'migrations/001_sensitive_words.sql').read_text()
            for statement in migration.split(';'):
                if statement.strip():
                    await cursor.execute(statement)
        settings = SimpleNamespace(host='127.0.0.1', port=int(os.environ['SENSITIVE_TEST_MYSQL_PORT']), user='root', password='isolated-test-password', database=self.database, connect_timeout=3, pool_max_size=4)
        self.repo = SensitiveWordRepository(settings)
        await self.repo.start()

    async def asyncTearDown(self):
        await self.repo.close()
        async with self.connection.cursor() as cursor:
            await cursor.execute('DROP DATABASE ' + self.database)
        self.connection.close()

    async def category(self, **overrides):
        return (await self.repo.create_category('agent', dict(name='分类', **overrides)))['item']['id']

    async def test_blocked_database_read_has_a_bounded_timeout(self):
        from common.sensitive_errors import SensitiveWordError
        from repositories.sensitive_word_repository import SensitiveWordRepository
        settings = SimpleNamespace(**vars(self.repo.settings))
        settings.query_timeout = 0.1
        waiting = SensitiveWordRepository(settings)
        try:
            await self.connection.begin()
            async with self.connection.cursor() as cursor:
                await cursor.execute("SELECT version FROM sensitive_word_versions WHERE scope='agent' FOR UPDATE")
            started = asyncio.get_running_loop().time()
            with self.assertRaises(SensitiveWordError) as caught:
                await asyncio.wait_for(waiting.load_snapshot('agent'), timeout=2)
            self.assertEqual(caught.exception.status_code, 503)
            self.assertLess(asyncio.get_running_loop().time() - started, 1)
            await self.connection.rollback()
            self.assertEqual((await waiting.load_snapshot('agent'))['version'], 0)
        finally:
            await self.connection.rollback()
            await waiting.close()

    async def test_cancellation_releases_connection_and_preserves_cancelled_error(self):
        await self.connection.begin()
        async with self.connection.cursor() as cursor:
            await cursor.execute("SELECT version FROM sensitive_word_versions WHERE scope='agent' FOR UPDATE")
        pending = asyncio.create_task(self.repo.load_snapshot('agent'))
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), timeout=0.05)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        finally:
            await self.connection.rollback()
            if not pending.done():
                pending.cancel()
        self.assertEqual((await self.repo.load_snapshot('agent'))['version'], 0)

    async def test_snapshot_requires_enabled_category_and_word_and_mutations_version(self):
        category = await self.category()
        created = await self.repo.create_word('agent', {'category_id':category,'word':'ＢＬＯＣＫ'})
        self.assertEqual(created['version'], 2)
        self.assertEqual((await self.repo.load_snapshot('agent'))['words'], ['block'])
        await self.repo.update_category('agent', category, {'status':0})
        self.assertEqual((await self.repo.load_snapshot('agent'))['words'], [])
        no_change = await self.repo.update_category('agent', category, {'status':0})
        self.assertEqual(no_change['version'], 3)
        await self.repo.update_category('agent', category, {'status':1})
        await self.repo.delete_word('agent', created['item']['id'])
        self.assertEqual((await self.repo.list_words('agent'))['total'], 0)
        self.assertEqual((await self.repo.load_snapshot('agent'))['version'], 5)

    async def test_same_normalized_word_is_allowed_in_distinct_categories(self):
        first = await self.category()
        second = (await self.repo.create_category('agent', {'name':'第二分类'}))['item']['id']
        await self.repo.create_word('agent', {'category_id':first,'word':'ＢＬＯＣＫ'})
        await self.repo.create_word('agent', {'category_id':second,'word':'block'})
        self.assertEqual((await self.repo.list_words('agent'))['total'], 2)
        self.assertEqual((await self.repo.load_snapshot('agent'))['words'], ['block'])
        await self.repo.update_category('agent', first, {'status':0})
        self.assertEqual((await self.repo.load_snapshot('agent'))['words'], ['block'])

    async def test_normalized_duplicate_race_and_validation(self):
        from common.sensitive_errors import SensitiveWordError
        category = await self.category()
        results = await asyncio.gather(*(self.repo.create_word('agent', {'category_id':category,'word':word}) for word in ('ＢＬＯＣＫ', 'block')), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, SensitiveWordError) for r in results), 1)
        self.assertEqual((await self.repo.load_snapshot('agent'))['version'], 2)
        for word in (' ', 'a'*256):
            with self.assertRaises(SensitiveWordError):
                await self.repo.create_word('agent', {'category_id':category,'word':word})
        with self.assertRaises(SensitiveWordError):
            await self.repo.create_word('agent', {'category_id':999999,'word':'x'})
        with self.assertRaises(SensitiveWordError):
            await self.repo.create_word('other', {'category_id':category,'word':'x'})

    async def test_pagination_filter_and_category_soft_delete(self):
        category = await self.category()
        await self.repo.create_word('agent', {'category_id':category,'word':'测试词'})
        await self.repo.create_word('agent', {'category_id':category,'word':'disabled','status':0})
        self.assertEqual((await self.repo.list_words('agent', page_size=1))['total'], 2)
        self.assertEqual(len((await self.repo.list_words('agent', page_size=1))['records']), 1)
        self.assertEqual((await self.repo.list_words('agent', word='测试', category_id=category, status=1))['total'], 1)
        await self.repo.delete_category('agent', category)
        self.assertEqual(await self.repo.list_categories('agent'), [])
        self.assertEqual((await self.repo.load_snapshot('agent'))['words'], [])

    def snapshot(self):
        return {'schema_version':1,'source_system':'legacy','scope':'agent','exported_at':'2026-09-04T00:00:00Z', 'categories':[{'source_id':'c1','name':'导入分类','status':1}], 'words':[{'source_id':'w1','category_source_id':'c1','word':'新词','status':1},{'source_id':'w2','category_source_id':'c1','word':'停用词','status':0}]}

    async def test_import_accepts_same_word_in_separate_categories(self):
        snapshot = self.snapshot()
        snapshot['categories'].append({'source_id':'c2','name':'其他分类','status':1})
        snapshot['words'].append({'source_id':'w3','category_source_id':'c2','word':'新词','status':1})
        preview = await self.repo.import_snapshot('agent', snapshot)
        self.assertEqual(preview['created_words'], 3)
        result = await self.repo.import_snapshot('agent', snapshot, dry_run=False)
        self.assertEqual(result['created_words'], 3)
        self.assertEqual((await self.repo.load_snapshot('agent'))['words'], ['新词'])

    async def test_import_dry_run_idempotency_and_preserves_local_edits_and_deleted_source(self):
        snapshot = self.snapshot()
        preview = await self.repo.import_snapshot('agent', snapshot)
        self.assertEqual(preview['created_words'], 2)
        self.assertEqual(await self.repo.list_categories('agent'), [])
        result = await self.repo.import_snapshot('agent', snapshot, dry_run=False)
        self.assertEqual(result['version'], 1)
        words = (await self.repo.list_words('agent'))['records']
        await self.repo.update_word('agent', words[0]['id'], {'word':'本地修改'})
        await self.repo.delete_word('agent', words[1]['id'])
        repeat = await self.repo.import_snapshot('agent', snapshot, dry_run=False)
        self.assertEqual(repeat['skipped_words'], 2)
        self.assertEqual(repeat['version'], 3)
        self.assertEqual((await self.repo.list_words('agent'))['records'][0]['word'], '本地修改')

    async def test_dry_run_works_with_select_only_database_account(self):
        from repositories.sensitive_word_repository import SensitiveWordRepository
        category = await self.category()
        username = 'preview_' + uuid.uuid4().hex[:16]
        async with self.connection.cursor() as cursor:
            await cursor.execute('CREATE USER %s IDENTIFIED BY %s', (username, 'preview-test-password'))
            await cursor.execute('GRANT SELECT ON ' + self.database + '.* TO %s', (username,))
        settings = SimpleNamespace(**vars(self.repo.settings))
        settings.user = username
        settings.password = 'preview-test-password'
        readonly = SensitiveWordRepository(settings)
        try:
            snapshot_result = await readonly.import_snapshot('agent', self.snapshot(), dry_run=True)
            csv_result = await readonly.import_words('agent', category, ['ＡＢＣ','abc'], dry_run=True)
            self.assertEqual(snapshot_result['created_words'], 2)
            self.assertEqual(csv_result['created_words'], 1)
            self.assertEqual(csv_result['skipped_words'], 1)
        finally:
            await readonly.close()
            async with self.connection.cursor() as cursor:
                await cursor.execute('DROP USER %s', (username,))

    async def test_dry_run_does_not_consume_auto_increment_ids(self):
        async with self.connection.cursor() as cursor:
            await cursor.execute('SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s', (self.database, 'sensitive_word_categories'))
            before = (await cursor.fetchone())[0]
        await self.repo.import_snapshot('agent', self.snapshot(), dry_run=True)
        async with self.connection.cursor() as cursor:
            await cursor.execute('SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s', (self.database, 'sensitive_word_categories'))
            after = (await cursor.fetchone())[0]
        self.assertEqual(before, after)

    async def test_late_import_conflict_rolls_back_earlier_category_insert(self):
        from common.sensitive_errors import SensitiveWordError
        snapshot = self.snapshot()
        initial = dict(snapshot, words=[])
        await self.repo.import_snapshot('agent', initial, dry_run=False)
        category = (await self.repo.list_categories('agent'))[0]['id']
        await self.repo.create_word('agent', {'category_id':category,'word':'新词'})
        snapshot['categories'].append({'source_id':'c2','name':'回滚分类','status':1})
        with self.assertRaises(SensitiveWordError) as raised:
            await self.repo.import_snapshot('agent', snapshot, dry_run=False)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(await self.repo.list_categories('agent')), 1)
        self.assertEqual((await self.repo.load_snapshot('agent'))['version'], 2)

    async def test_import_conflict_rolls_back_and_csv_deduplicates(self):
        from common.sensitive_errors import SensitiveWordError
        category = (await self.repo.create_category('agent', {'name':'导入分类'}))['item']['id']
        await self.repo.create_word('agent', {'category_id':category,'word':'新词'})
        with self.assertRaises(SensitiveWordError):
            await self.repo.import_snapshot('agent', self.snapshot(), dry_run=False)
        self.assertEqual(len(await self.repo.list_categories('agent')), 1)
        self.assertEqual((await self.repo.load_snapshot('agent'))['version'], 2)
        preview = await self.repo.import_words('agent', category, ['ＡＢＣ','abc','新词'])
        self.assertEqual(preview['created_words'], 1)
        self.assertEqual(preview['skipped_words'], 2)
        self.assertEqual((await self.repo.list_words('agent'))['total'], 1)
        result = await self.repo.import_words('agent', category, ['ＡＢＣ','abc'], dry_run=False)
        self.assertEqual(result['version'], 3)
