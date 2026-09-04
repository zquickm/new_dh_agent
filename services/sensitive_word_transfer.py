"""Explicit one-time transfer of an agent dictionary; never writes to the source."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

import aiohttp


class TransferError(Exception):
    """Public error containing no remote response or credential details."""

    def __init__(self, message: str, *, persisted: bool = False):
        super().__init__(message)
        self.persisted = persisted


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransferError('Invalid snapshot field: ' + field)
    return value


def _id(value: Any) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return _text(value, 'source ID')


def _status(value: Any) -> int:
    if type(value) is not int or value not in (0, 1):
        raise TransferError('Invalid dictionary status; expected 0 or 1')
    return value


def validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or type(snapshot.get('schema_version')) is not int or snapshot['schema_version'] != 1:
        raise TransferError('Unsupported snapshot schema')
    if snapshot.get('scope') != 'agent':
        raise TransferError('Snapshot scope must be agent')
    _text(snapshot.get('source_system'), 'source_system')
    try:
        stamp = datetime.fromisoformat(_text(snapshot.get('exported_at'), 'exported_at').replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise TransferError('Invalid snapshot timestamp') from None
    categories, words = snapshot.get('categories'), snapshot.get('words')
    if not isinstance(categories, list) or not categories or not isinstance(words, list):
        raise TransferError('Snapshot needs a nonempty category list and a word list')
    category_ids, word_ids = set(), set()
    for category in categories:
        if not isinstance(category, dict):
            raise TransferError('Malformed snapshot category')
        category_id = _text(category.get('source_id'), 'category source_id')
        if category_id in category_ids:
            raise TransferError('Duplicate category source ID')
        category_ids.add(category_id)
        _text(category.get('name'), 'category name')
        _status(category.get('status'))
    for word in words:
        if not isinstance(word, dict):
            raise TransferError('Malformed snapshot word')
        word_id = _text(word.get('source_id'), 'word source_id')
        if word_id in word_ids:
            raise TransferError('Duplicate word source ID')
        word_ids.add(word_id)
        if _text(word.get('category_source_id'), 'category_source_id') not in category_ids:
            raise TransferError('Word refers to an unknown category')
        _text(word.get('word'), 'word')
        _status(word.get('status'))
    return snapshot


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise TransferError('Unable to read a valid JSON snapshot') from None
    return validate_snapshot(snapshot)


async def _request(session: aiohttp.ClientSession, url: str, params: Optional[dict[str, int]] = None) -> Any:
    try:
        # A redirect could forward X-Access-Token outside the configured source.
        async with session.get(url, params=params, allow_redirects=False) as response:
            if response.status != 200:
                raise TransferError('Source request failed (HTTP %d)' % response.status)
            try:
                body = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                raise TransferError('Source returned invalid JSON') from None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise TransferError('Source connection failed') from None
    if not isinstance(body, dict) or body.get('success') is not True or type(body.get('code')) is not int or body['code'] != 200 or 'result' not in body:
        raise TransferError('Source rejected request or returned an invalid envelope')
    return body['result']


async def _read_source(session: aiohttp.ClientSession, base_url: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_categories = await _request(session, base_url + '/agent/sensitive/category/list')
    if not isinstance(raw_categories, list) or not raw_categories:
        raise TransferError('Source returned no valid category list')
    categories = []
    for category in raw_categories:
        if not isinstance(category, dict) or category.get('scope') != 'agent':
            raise TransferError('Source category has unrelated or missing scope')
        categories.append({'source_id': _id(category.get('id')), 'name': _text(category.get('categoryName'), 'categoryName'), 'status': _status(category.get('status'))})
    words = []
    expected_total = None
    page = 1
    while True:
        result = await _request(session, base_url + '/agent/sensitive/word/list', {'pageNo': page, 'pageSize': 100})
        if not isinstance(result, dict) or not isinstance(result.get('records'), list):
            raise TransferError('Source returned malformed word page')
        total, current, size, pages = (result.get(key) for key in ('total', 'current', 'size', 'pages'))
        if any(type(value) is not int for value in (total, current, size, pages)) or total < 0 or current != page or size != 100:
            raise TransferError('Source pagination metadata is invalid')
        if pages != (total + 99) // 100 and not (total == 0 and pages == 1):
            raise TransferError('Source page count is inconsistent')
        if expected_total is not None and total != expected_total:
            raise TransferError('Source total changed during export')
        expected_total = total
        records = result['records']
        if len(records) != min(100, max(0, total - (page - 1) * 100)):
            raise TransferError('Source pagination is incomplete')
        for word in records:
            if not isinstance(word, dict) or word.get('scope', 'agent') != 'agent':
                raise TransferError('Source returned an invalid word or unrelated scope')
            words.append({'source_id': _id(word.get('id')), 'category_source_id': _id(word.get('categoryId')), 'word': _text(word.get('word'), 'word'), 'status': _status(word.get('status'))})
        if page >= pages:
            break
        page += 1
    return sorted(categories, key=lambda value: value['source_id']), sorted(words, key=lambda value: value['source_id'])


def _atomic_write(path: Path, snapshot: dict[str, Any]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.' + path.name + '.', delete=False) as file:
            temporary = file.name
            json.dump(snapshot, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except OSError:
        raise TransferError('Unable to write snapshot atomically') from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


async def export_snapshot(base_url: str, token: str, source_system: str, output: Path) -> dict[str, Any]:
    """Read two matching complete snapshots, then atomically publish a local JSON file."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.endswith('.git'):
        raise TransferError('SOURCE_SENSITIVE_BASE_URL must be a deployed HTTP API base URL')
    if not token or '\r' in token or '\n' in token:
        raise TransferError('SOURCE_SENSITIVE_TOKEN is missing or invalid')
    _text(source_system, 'source_system')
    async with aiohttp.ClientSession(headers={'X-Access-Token': token}, timeout=aiohttp.ClientTimeout(total=30)) as session:
        first = await _read_source(session, base_url.rstrip('/'))
        snapshot = {'schema_version': 1, 'source_system': source_system, 'scope': 'agent', 'exported_at': datetime.now(timezone.utc).isoformat(), 'categories': first[0], 'words': first[1]}
        validate_snapshot(snapshot)
        second = await _read_source(session, base_url.rstrip('/'))
        if first != second:
            raise TransferError('Source dictionary changed; export again while source editing is paused')
    _atomic_write(Path(output), snapshot)
    return snapshot


async def import_snapshot(repository: Any, snapshot: dict[str, Any], *, apply: bool = False, refresh: Optional[Callable[[str], Awaitable[Any]]] = None) -> dict[str, Any]:
    """Commit locally only when requested; cache publication follows a successful commit."""
    try:
        result = await repository.import_snapshot('agent', snapshot, dry_run=not apply)
    except Exception:
        raise TransferError('Local dictionary import failed') from None
    if apply:
        try:
            if refresh is None:
                raise RuntimeError('Cache refresh is required')
            await refresh('agent')
        except Exception:
            raise TransferError('Import committed, but cache refresh failed; retry cache refresh before serving traffic', persisted=True) from None
    return {**result, 'persisted': apply}
