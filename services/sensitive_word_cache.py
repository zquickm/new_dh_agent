"""Versioned shared snapshots with a per-worker last-known-good fallback."""
from __future__ import annotations

import json
import logging
from copy import deepcopy

from common.sensitive_errors import SensitiveWordError
from common.text_normalization import normalize_text

logger = logging.getLogger(__name__)


# Compare inside Redis so delayed readers can never replace a newer publication.
_PUBLISH = """
local current = redis.call('GET', KEYS[1])
if current and current ~= ARGV[4] then
  local ok, parsed = pcall(cjson.decode, current)
  if ok and type(parsed) == 'table' and type(parsed.version) == 'number'
     and parsed.scope == ARGV[3] and type(parsed.words) == 'table'
     and parsed.version > tonumber(ARGV[1]) then
    return 0
  end
end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""


class SensitiveWordCache:
    def __init__(self, repository, redis_client, *, key_prefix='new_dh_agent:sensitive'):
        self.repository = repository
        self.redis = redis_client
        self.key_prefix = key_prefix.rstrip(':')
        self._snapshots = {}

    @staticmethod
    def _validate(snapshot, scope):
        if scope != 'agent':
            raise SensitiveWordError('Unsupported sensitive-word scope')
        if (not isinstance(snapshot, dict) or snapshot.get('scope') != scope
                or type(snapshot.get('version')) is not int or snapshot['version'] < 0
                or not isinstance(snapshot.get('words'), list)
                or any(not isinstance(word, str) or not word.strip() or normalize_text(word) != word for word in snapshot['words'])):
            raise SensitiveWordError('Invalid sensitive-word snapshot', status_code=503)
        return {key: deepcopy(snapshot[key]) for key in ('scope', 'version', 'words')}

    def _remember(self, snapshot, scope):
        snapshot = self._validate(snapshot, scope)
        existing = self._snapshots.get(scope)
        if existing is None or snapshot['version'] >= existing['version']:
            self._snapshots[scope] = snapshot
        return deepcopy(self._snapshots[scope])

    async def _database(self, scope):
        try:
            return self._validate(await self.repository.load_snapshot(scope), scope)
        except Exception:
            raise SensitiveWordError('Sensitive-word storage unavailable', status_code=503) from None

    async def _publish(self, snapshot, scope):
        try:
            rejected_payload = ''
            for _ in range(3):
                result = await self.redis.eval(
                    _PUBLISH, 1, self.key_prefix + ':' + scope,
                    str(snapshot['version']), json.dumps(snapshot, ensure_ascii=False),
                    scope, rejected_payload,
                )
                if result != 0:
                    return self._remember(snapshot, scope)
                current_payload = await self.redis.get(self.key_prefix + ':' + scope)
                try:
                    current = self._validate(json.loads(current_payload), scope)
                except (ValueError, TypeError, SensitiveWordError):
                    # Replace only this exact rejected blob: a concurrent valid
                    # publication must still be protected by the version check.
                    rejected_payload = current_payload if current_payload is not None else ''
                    logger.warning('Sensitive-word Redis snapshot invalid; repairing from database')
                    continue
                return self._remember(current, scope)
        except Exception:
            pass
        raise SensitiveWordError('Sensitive-word cache publication failed', status_code=503) from None

    async def get_snapshot(self, scope):
        if scope != 'agent':
            raise SensitiveWordError('Unsupported sensitive-word scope')
        try:
            cached = await self.redis.get(self.key_prefix + ':' + scope)
        except Exception:
            logger.warning('Sensitive-word Redis read unavailable; using fallback snapshot')
            if scope in self._snapshots:
                return deepcopy(self._snapshots[scope])
            cached = None
        if cached is not None:
            try:
                return self._remember(json.loads(cached), scope)
            except (ValueError, TypeError, SensitiveWordError):
                logger.warning('Sensitive-word Redis snapshot invalid; loading database snapshot')
        try:
            snapshot = await self._database(scope)
        except SensitiveWordError:
            logger.warning('Sensitive-word database unavailable; retaining last valid snapshot when available')
            if scope in self._snapshots:
                return deepcopy(self._snapshots[scope])
            raise
        self._remember(snapshot, scope)
        try:
            return await self._publish(snapshot, scope)
        except SensitiveWordError:
            logger.warning('Sensitive-word Redis publication unavailable; using local database snapshot')
            return deepcopy(self._snapshots[scope])

    async def refresh(self, scope):
        snapshot = await self._database(scope)
        self._remember(snapshot, scope)
        return await self._publish(snapshot, scope)

    async def start(self, scope='agent'):
        await self.get_snapshot(scope)

    async def close(self):
        # Redis and repository clients are owned by the application lifespan.
        self._snapshots.clear()
