"""Dedicated MySQL store; apply migrations/001_sensitive_words.sql explicitly."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256

import asyncmy
from async_timeout import timeout
from asyncmy.cursors import DictCursor

from common.sensitive_errors import SensitiveWordError
from common.text_normalization import normalize_text


class _TimedCursor:
    """Bound complete commands; DictCursor fetches already-buffered rows."""

    def __init__(self, cursor, seconds):
        self._cursor = cursor
        self._seconds = seconds

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    async def execute(self, *args):
        async with timeout(self._seconds):
            return await self._cursor.execute(*args)


class SensitiveWordRepository:
    def __init__(self, mysql_settings):
        self.settings = mysql_settings
        self._pool = None
        self._start_lock = asyncio.Lock()

    async def start(self):
        async with self._start_lock:
            if self._pool is not None:
                return
            settings = self.settings
            password = settings.password
            if hasattr(password, 'get_secret_value'):
                password = password.get_secret_value()
            try:
                self._pool = await self._bounded(asyncmy.create_pool(
                    host=settings.host, port=settings.port, user=settings.user,
                    password=password, db=settings.database, charset='utf8mb4',
                    connect_timeout=settings.connect_timeout, minsize=1,
                    maxsize=settings.pool_max_size, autocommit=False,
                ), settings.connect_timeout)
            except Exception:
                raise SensitiveWordError('Sensitive-word database unavailable', status_code=503) from None

    async def _bounded(self, operation, seconds=None):
        async with timeout(seconds if seconds is not None else getattr(self.settings, 'query_timeout', 10)):
            return await operation

    @asynccontextmanager
    async def _connection(self):
        connection = await self._bounded(self._pool.acquire(), self.settings.connect_timeout)
        try:
            yield connection
        except (asyncio.TimeoutError, asyncio.CancelledError):
            connection.close()
            raise
        finally:
            await self._pool.release(connection)

    async def close(self):
        async with self._start_lock:
            if self._pool is not None:
                self._pool.close()
                await self._pool.wait_closed()
                self._pool = None

    @staticmethod
    def _scope(scope):
        if scope != 'agent':
            raise SensitiveWordError('Unsupported sensitive-word scope')

    @staticmethod
    def _id(value):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise SensitiveWordError('Invalid record ID')
        try:
            parsed = int(value)
        except (ValueError, TypeError):
            raise SensitiveWordError('Invalid record ID') from None
        if parsed < 1 or parsed > 18446744073709551615:
            raise SensitiveWordError('Invalid record ID')
        return parsed

    @staticmethod
    def _status(value):
        if type(value) is not int or value not in (0, 1):
            raise SensitiveWordError('Status must be 0 or 1')
        return value

    @staticmethod
    def _text(value, label, limit):
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise SensitiveWordError(f'{label} must contain 1 to {limit} characters')
        value = value.strip()
        normalized = normalize_text(value)
        if not normalized.strip() or len(normalized) > 1020:
            raise SensitiveWordError(f'Invalid normalized {label.lower()}')
        return value, normalized, sha256(normalized.encode('utf-8')).digest()

    @staticmethod
    def _item(row):
        return {key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in row.items() if key not in ('normalized_hash', 'live_hash')}

    @asynccontextmanager
    async def _transaction(self, scope, *, dry_run=False):
        self._scope(scope)
        await self.start()
        try:
            async with self._connection() as connection:
                if dry_run:
                    async with connection.cursor() as transaction_cursor:
                        transaction_cursor = _TimedCursor(transaction_cursor, getattr(self.settings, 'query_timeout', 10))
                        await transaction_cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                        await transaction_cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                else:
                    await self._bounded(connection.begin())
                try:
                    async with connection.cursor(DictCursor) as cursor:
                        cursor = _TimedCursor(cursor, getattr(self.settings, 'query_timeout', 10))
                        # Mutations and live reads share a version lock; previews use a
                        # read-only consistent snapshot and need only SELECT privileges.
                        lock = '' if dry_run else ' FOR UPDATE'
                        await cursor.execute('SELECT version FROM sensitive_word_versions WHERE scope=%s' + lock, (scope,))
                        row = await cursor.fetchone()
                        if row is None:
                            raise SensitiveWordError('Sensitive-word schema migration required', status_code=503)
                        yield cursor, int(row['version'])
                    if dry_run:
                        await self._bounded(connection.rollback())
                    else:
                        await self._bounded(connection.commit())
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    # Discard an interrupted command so its late response cannot
                    # be consumed by the next borrower of this connection.
                    connection.close()
                    raise
                except BaseException:
                    try:
                        await self._bounded(connection.rollback())
                    except Exception:
                        # A timed-out/cancelled connection may already be closed.
                        # Preserve the original failure, including cancellation.
                        connection.close()
                    raise
        except SensitiveWordError:
            raise
        except asyncmy.errors.IntegrityError:
            raise SensitiveWordError('Sensitive-word record conflicts with existing data', status_code=409) from None
        except Exception:
            raise SensitiveWordError('Sensitive-word database operation failed', status_code=503) from None

    async def _advance(self, cursor, scope, version, changed):
        if changed:
            await cursor.execute('UPDATE sensitive_word_versions SET version=version+1 WHERE scope=%s', (scope,))
        return version + int(bool(changed))

    async def _record(self, cursor, table, scope, identifier):
        await cursor.execute(f'SELECT * FROM {table} WHERE scope=%s AND id=%s AND deleted_at IS NULL', (scope, self._id(identifier)))
        row = await cursor.fetchone()
        if row is None:
            raise SensitiveWordError('Sensitive-word record not found', status_code=404)
        return row

    async def load_snapshot(self, scope):
        async with self._transaction(scope) as (cursor, version):
            await cursor.execute('''SELECT w.normalized_word FROM sensitive_words w
                JOIN sensitive_word_categories c ON w.category_id=c.id AND c.scope=w.scope
                WHERE w.scope=%s AND w.status=1 AND c.status=1
                  AND w.deleted_at IS NULL AND c.deleted_at IS NULL ORDER BY w.id''', (scope,))
            return {'scope':scope, 'version':version, 'words':list(dict.fromkeys(row['normalized_word'] for row in await cursor.fetchall()))}

    async def list_categories(self, scope):
        async with self._transaction(scope) as (cursor, _):
            await cursor.execute('SELECT * FROM sensitive_word_categories WHERE scope=%s AND deleted_at IS NULL ORDER BY id', (scope,))
            return [self._item(row) for row in await cursor.fetchall()]

    async def list_words(self, scope, *, page=1, page_size=50, word=None, category_id=None, status=None):
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200:
            raise SensitiveWordError('Invalid pagination')
        conditions = ['w.scope=%s', 'w.deleted_at IS NULL', 'c.deleted_at IS NULL']
        params = [scope]
        if word is not None:
            _, normalized, _ = self._text(word, 'Word filter', 255)
            conditions.append('LOCATE(%s, w.normalized_word)>0')
            params.append(normalized)
        if category_id is not None:
            conditions.append('w.category_id=%s')
            params.append(self._id(category_id))
        if status is not None:
            conditions.append('w.status=%s')
            params.append(self._status(status))
        base = ' FROM sensitive_words w JOIN sensitive_word_categories c ON c.id=w.category_id AND c.scope=w.scope WHERE ' + ' AND '.join(conditions)
        async with self._transaction(scope) as (cursor, _):
            await cursor.execute('SELECT COUNT(*) AS total' + base, params)
            total = (await cursor.fetchone())['total']
            await cursor.execute('SELECT w.*, c.name AS category_name' + base + ' ORDER BY w.id LIMIT %s OFFSET %s', params + [page_size, (page-1)*page_size])
            return {'records':[self._item(row) for row in await cursor.fetchall()], 'total':total,'page':page,'page_size':page_size}

    async def _insert_category(self, cursor, scope, values, source_system=None, source_id=None):
        name, _, digest = self._text(values.get('name'), 'Category name', 128)
        status = self._status(values.get('status', 1))
        await cursor.execute('''INSERT INTO sensitive_word_categories
            (scope,name,normalized_hash,status,source_system,source_id) VALUES (%s,%s,%s,%s,%s,%s)''',
            (scope,name,digest,status,source_system,source_id))
        return await self._record(cursor, 'sensitive_word_categories', scope, cursor.lastrowid)

    async def _insert_word(self, cursor, scope, values, source_system=None, source_id=None):
        word, normalized, digest = self._text(values.get('word'), 'Word', 255)
        category_id = self._id(values.get('category_id'))
        await self._record(cursor, 'sensitive_word_categories', scope, category_id)
        status = self._status(values.get('status', 1))
        await cursor.execute('''INSERT INTO sensitive_words
            (scope,category_id,word,normalized_word,normalized_hash,status,source_system,source_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (scope,category_id,word,normalized,digest,status,source_system,source_id))
        return await self._record(cursor, 'sensitive_words', scope, cursor.lastrowid)

    async def create_category(self, scope, values):
        async with self._transaction(scope) as (cursor, version):
            row = await self._insert_category(cursor, scope, values)
            return {'item':self._item(row),'version':await self._advance(cursor,scope,version,True)}

    async def create_word(self, scope, values):
        async with self._transaction(scope) as (cursor, version):
            row = await self._insert_word(cursor, scope, values)
            return {'item':self._item(row),'version':await self._advance(cursor,scope,version,True)}

    async def _update(self, scope, identifier, values, *, category):
        allowed = {'name','status'} if category else {'word','category_id','status'}
        if not isinstance(values, dict) or not values or set(values) - allowed:
            raise SensitiveWordError('Invalid update fields')
        table = 'sensitive_word_categories' if category else 'sensitive_words'
        async with self._transaction(scope) as (cursor, version):
            row = await self._record(cursor, table, scope, identifier)
            changes = {}
            field = 'name' if category else 'word'
            if field in values:
                value, normalized, digest = self._text(values[field], 'Category name' if category else 'Word', 128 if category else 255)
                changes.update({field:value,'normalized_hash':digest})
                if not category:
                    changes['normalized_word'] = normalized
            if 'status' in values:
                changes['status'] = self._status(values['status'])
            if 'category_id' in values:
                changes['category_id'] = self._id(values['category_id'])
                await self._record(cursor, 'sensitive_word_categories', scope, changes['category_id'])
            changed = any(row[key] != value for key, value in changes.items())
            if changed:
                await cursor.execute(f'UPDATE {table} SET ' + ','.join(key+'=%s' for key in changes) + ' WHERE scope=%s AND id=%s', list(changes.values())+[scope,row['id']])
                row = await self._record(cursor, table, scope, row['id'])
            return {'item':self._item(row),'version':await self._advance(cursor,scope,version,changed)}

    async def update_category(self, scope, identifier, values):
        return await self._update(scope, identifier, values, category=True)

    async def update_word(self, scope, identifier, values):
        return await self._update(scope, identifier, values, category=False)

    async def _delete(self, scope, identifier, *, category):
        table = 'sensitive_word_categories' if category else 'sensitive_words'
        async with self._transaction(scope) as (cursor, version):
            row = await self._record(cursor, table, scope, identifier)
            await cursor.execute(f'UPDATE {table} SET deleted_at=CURRENT_TIMESTAMP(6) WHERE scope=%s AND id=%s', (scope,row['id']))
            if category:
                await cursor.execute('UPDATE sensitive_words SET deleted_at=CURRENT_TIMESTAMP(6) WHERE scope=%s AND category_id=%s AND deleted_at IS NULL', (scope,row['id']))
            await cursor.execute(f'SELECT * FROM {table} WHERE id=%s', (row['id'],))
            return {'item':self._item(await cursor.fetchone()),'version':await self._advance(cursor,scope,version,True)}

    async def delete_category(self, scope, identifier):
        return await self._delete(scope, identifier, category=True)

    async def delete_word(self, scope, identifier):
        return await self._delete(scope, identifier, category=False)

    async def import_words(self, scope, category_id, words, *, dry_run=True):
        if not isinstance(words, list) or type(dry_run) is not bool:
            raise SensitiveWordError('Invalid word import')
        validated = [self._text(word, 'Word', 255) for word in words]
        async with self._transaction(scope, dry_run=dry_run) as (cursor, version):
            category = await self._record(cursor, 'sensitive_word_categories', scope, category_id)
            created = skipped = 0
            seen = set()
            for word, _, digest in validated:
                await cursor.execute('SELECT id FROM sensitive_words WHERE scope=%s AND category_id=%s AND live_hash=%s', (scope,category['id'],digest))
                if await cursor.fetchone() or digest in seen:
                    skipped += 1
                else:
                    if not dry_run:
                        await self._insert_word(cursor, scope, {'word':word,'category_id':category['id']})
                    seen.add(digest)
                    created += 1
            new_version = version if dry_run else await self._advance(cursor,scope,version,created > 0)
            return {'created_words':created,'skipped_words':skipped,'version':version if dry_run else new_version,'dry_run':dry_run}

    def _validate_import(self, scope, snapshot):
        self._scope(scope)
        if (not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int
                or snapshot['schema_version'] != 1 or snapshot.get('scope') != scope
                or not isinstance(snapshot.get('exported_at'), str) or not snapshot['exported_at'].strip()
                or not isinstance(snapshot.get('categories'), list) or not isinstance(snapshot.get('words'), list)):
            raise SensitiveWordError('Invalid sensitive-word import snapshot')
        source, _, _ = self._text(snapshot.get('source_system'), 'Source system', 128)
        category_ids = set()
        word_ids = set()
        for collection, field, limit, seen in ((snapshot['categories'],'name',128,category_ids),(snapshot['words'],'word',255,word_ids)):
            for row in collection:
                if not isinstance(row, dict):
                    raise SensitiveWordError('Invalid source record')
                source_id, _, _ = self._text(row.get('source_id'), 'Source ID', 191)
                if source_id != row['source_id'] or source_id in seen:
                    raise SensitiveWordError('Duplicate or invalid source ID')
                seen.add(source_id)
                self._text(row.get(field), field, limit)
                self._status(row.get('status'))
        for row in snapshot['words']:
            if not isinstance(row.get('category_source_id'), str) or row['category_source_id'] not in category_ids:
                raise SensitiveWordError('Unknown source category relationship')
        return source

    async def import_snapshot(self, scope, snapshot, *, dry_run=True):
        source = self._validate_import(scope, snapshot)
        if type(dry_run) is not bool:
            raise SensitiveWordError('Invalid dry_run value')
        stats = {'created_categories':0,'created_words':0,'skipped_categories':0,'skipped_words':0}
        async with self._transaction(scope, dry_run=dry_run) as (cursor, version):
            categories = {}
            preview_category_hashes = set()
            preview_word_hashes = set()
            for row in snapshot['categories']:
                await cursor.execute('SELECT * FROM sensitive_word_categories WHERE scope=%s AND source_system=%s AND source_id=%s', (scope,source,row['source_id']))
                existing = await cursor.fetchone()
                if existing:
                    stats['skipped_categories'] += 1
                    categories[row['source_id']] = existing
                else:
                    if dry_run:
                        _, _, digest = self._text(row['name'], 'Category name', 128)
                        await self._check_preview_unique(cursor, 'sensitive_word_categories', scope, digest, preview_category_hashes)
                        categories[row['source_id']] = {'id':None, 'deleted_at':None}
                    else:
                        categories[row['source_id']] = await self._insert_category(cursor, scope, row, source, row['source_id'])
                    stats['created_categories'] += 1
            for row in snapshot['words']:
                await cursor.execute('SELECT id FROM sensitive_words WHERE scope=%s AND source_system=%s AND source_id=%s', (scope,source,row['source_id']))
                if await cursor.fetchone():
                    stats['skipped_words'] += 1
                    continue
                category = categories[row['category_source_id']]
                if category['deleted_at'] is not None:
                    raise SensitiveWordError('Source category was locally deleted', status_code=409)
                if dry_run:
                    _, _, digest = self._text(row['word'], 'Word', 255)
                    await self._check_preview_unique(cursor, 'sensitive_words', scope, digest, preview_word_hashes, category_id=category['id'], group=row['category_source_id'])
                else:
                    await self._insert_word(cursor, scope, dict(row, category_id=category['id']), source, row['source_id'])
                stats['created_words'] += 1
            changed = stats['created_categories'] + stats['created_words'] > 0
            new_version = version if dry_run else await self._advance(cursor,scope,version,changed)
            return dict(stats, version=version if dry_run else new_version, dry_run=dry_run)

    async def _check_preview_unique(self, cursor, table, scope, digest, seen, *, category_id=None, group=None):
        if table == 'sensitive_words':
            existing = None
            if category_id is not None:
                await cursor.execute('SELECT id FROM sensitive_words WHERE scope=%s AND category_id=%s AND live_hash=%s', (scope,category_id,digest))
                existing = await cursor.fetchone()
            key = (group, digest)
        else:
            await cursor.execute('SELECT id FROM sensitive_word_categories WHERE scope=%s AND live_hash=%s', (scope,digest))
            existing = await cursor.fetchone()
            key = digest
        if existing or key in seen:
            raise SensitiveWordError('Sensitive-word record conflicts with existing data', status_code=409)
        seen.add(key)
