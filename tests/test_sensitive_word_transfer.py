from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web

from services.sensitive_word_transfer import (
    TransferError,
    export_snapshot,
    load_snapshot,
    import_snapshot,
)


class ExportSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = 'private-test-token'
        self.categories = [
            {'id': 'c1', 'categoryName': '默认', 'scope': 'agent', 'status': 1, 'createBy': 'private-author'},
            {'id': 'c2', 'categoryName': '停用分类', 'scope': 'agent', 'status': 0},
        ]
        self.words = [
            {'id': str(i), 'categoryId': 'c1' if i < 100 else 'c2', 'word': '词' + str(i), 'status': i % 2}
            for i in range(101)
        ]
        self.requests = []
        self.mode = ''
        self.category_calls = 0

        async def category(request):
            self.requests.append(request)
            self.category_calls += 1
            if self.mode == 'unauthorized':
                return web.json_response({'message': self.token}, status=401)
            if self.mode == 'envelope':
                return web.json_response({'success': False, 'code': 200, 'message': self.token})
            data = copy.deepcopy(self.categories)
            if self.mode == 'changed' and self.category_calls > 1:
                data[0]['categoryName'] = 'changed'
            return web.json_response({'success': True, 'code': 200, 'result': data})

        async def word(request):
            self.requests.append(request)
            page = int(request.query['pageNo'])
            records = copy.deepcopy(self.words[(page - 1) * 100:page * 100])
            total = len(self.words)
            if self.mode == 'duplicate' and page == 2:
                records[0]['id'] = '0'
            if self.mode == 'missing' and page == 2:
                records = []
            if self.mode == 'total_changed' and page == 2:
                total += 1
            return web.json_response({'success': True, 'code': 200, 'result': {
                'records': records, 'total': total, 'current': page, 'size': 100,
                'pages': (total + 99) // 100,
            }})

        app = web.Application()
        app.router.add_get('/jeecg-boot/agent/sensitive/category/list', category)
        app.router.add_get('/jeecg-boot/agent/sensitive/word/list', word)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base_url = 'http://127.0.0.1:%s/jeecg-boot' % site._server.sockets[0].getsockname()[1]
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name) / 'snapshot.json'

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def export(self):
        return await export_snapshot(self.base_url, self.token, 'hk-ai-tutor', self.output)

    async def test_complete_export_preserves_statuses_and_context_path_without_secrets(self):
        snapshot = await self.export()
        self.assertEqual(len(snapshot['words']), 101)
        self.assertEqual(snapshot['categories'], [
            {'source_id': 'c1', 'name': '默认', 'status': 1},
            {'source_id': 'c2', 'name': '停用分类', 'status': 0},
        ])
        self.assertEqual(snapshot['words'][0], {'source_id': '0', 'category_source_id': 'c1', 'word': '词0', 'status': 0})
        self.assertEqual(snapshot['source_system'], 'hk-ai-tutor')
        self.assertEqual(snapshot['scope'], 'agent')
        self.assertEqual(snapshot['schema_version'], 1)
        self.assertEqual(load_snapshot(self.output), snapshot)
        output = self.output.read_text()
        self.assertNotIn(self.token, output)
        self.assertNotIn('private-author', output)
        self.assertEqual(len(self.requests), 6)
        for request in self.requests:
            self.assertEqual(request.headers['X-Access-Token'], self.token)
            self.assertNotIn('status', request.query)
            self.assertEqual(request.method, 'GET')

    async def test_bad_or_changing_source_never_replaces_existing_file(self):
        for mode in ('unauthorized', 'envelope', 'duplicate', 'missing', 'total_changed', 'changed'):
            with self.subTest(mode=mode):
                self.mode = mode
                self.category_calls = 0
                self.output.write_text('existing')
                with self.assertRaises(TransferError) as error:
                    await self.export()
                self.assertNotIn(self.token, str(error.exception))
                self.assertEqual(self.output.read_text(), 'existing')

    async def test_invalid_scope_status_reference_and_empty_categories_rejected(self):
        original_categories = copy.deepcopy(self.categories)
        original_words = copy.deepcopy(self.words)
        for case in ('scope', 'category_status', 'word_status', 'reference', 'empty', 'duplicate_category'):
            with self.subTest(case=case):
                self.categories = copy.deepcopy(original_categories)
                self.words = copy.deepcopy(original_words)
                if case == 'scope':
                    self.categories[0]['scope'] = 'other'
                elif case == 'category_status':
                    self.categories[0]['status'] = 7
                elif case == 'word_status':
                    self.words[0]['status'] = True
                elif case == 'reference':
                    self.words[0]['categoryId'] = 'missing'
                elif case == 'empty':
                    self.categories = []
                else:
                    self.categories.append(self.categories[0])
                with self.assertRaises(TransferError):
                    await self.export()
                self.assertFalse(self.output.exists())

    async def test_boolean_schema_version_and_non_scalar_reference_are_rejected(self):
        original = await self.export()
        for case in ('schema', 'reference'):
            with self.subTest(case=case):
                data = copy.deepcopy(original)
                if case == 'schema':
                    data['schema_version'] = True
                else:
                    data['words'][0]['category_source_id'] = []
                self.output.write_text(json.dumps(data))
                with self.assertRaises(TransferError):
                    load_snapshot(self.output)

    async def test_invalid_local_snapshot_rejected_before_import(self):
        await self.export()
        data = json.loads(self.output.read_text())
        data['words'][0]['category_source_id'] = 'absent'
        self.output.write_text(json.dumps(data))
        with self.assertRaises(TransferError):
            load_snapshot(self.output)


class ImportSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_does_not_publish_and_apply_reports_committed_cache_failure(self):
        # Database is the external boundary: the test double models its committed state.
        class Repository:
            persisted = False
            async def import_snapshot(self, scope, snapshot, *, dry_run=True):
                assert scope == 'agent'
                self.persisted = not dry_run
                return {'inserted': 1, 'version': 2}

        repo = Repository()
        refreshes = []
        async def refresh(scope):
            refreshes.append(scope)
            raise RuntimeError('secret-password')

        stats = await import_snapshot(repo, {'scope': 'agent'}, apply=False, refresh=refresh)
        self.assertFalse(repo.persisted)
        self.assertFalse(stats['persisted'])
        self.assertEqual(refreshes, [])
        with self.assertRaises(TransferError) as error:
            await import_snapshot(repo, {'scope': 'agent'}, apply=True, refresh=refresh)
        self.assertTrue(repo.persisted)
        self.assertTrue(error.exception.persisted)
        self.assertNotIn('secret-password', str(error.exception))

    def test_cli_help_works_without_database_or_source_configuration(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('export_sensitive_words.py', 'import_sensitive_words.py'):
            result = subprocess.run([sys.executable, str(root / 'scripts' / name), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)
