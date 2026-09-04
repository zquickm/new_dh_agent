#!/usr/bin/env python3
"""Import a reviewed JSON snapshot into local MySQL; default to a read-only dry run."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sensitive_word_transfer import TransferError, import_snapshot, load_snapshot


async def run(path: Path, apply: bool) -> dict:
    snapshot = load_snapshot(path)
    # Lazy imports keep --help and export independent of application configuration.
    from config.settings import get_settings
    from repositories.sensitive_word_repository import SensitiveWordRepository

    settings = get_settings().sensitive_words
    if settings.backend != 'mysql':
        raise TransferError('Set sensitive_words.backend=mysql before importing')
    repository = SensitiveWordRepository(settings.mysql)
    cache = None
    redis_client = None

    async def refresh(scope: str) -> None:
        nonlocal cache, redis_client
        from redis.asyncio import from_url
        from services.sensitive_word_cache import SensitiveWordCache

        redis_client = from_url(settings.redis.url.get_secret_value(), decode_responses=True, socket_timeout=settings.redis.socket_timeout, socket_connect_timeout=settings.redis.socket_timeout)
        cache = SensitiveWordCache(repository, redis_client, key_prefix=settings.redis.key_prefix)
        await cache.refresh(scope)

    try:
        await repository.start()
        return await import_snapshot(repository, snapshot, apply=apply, refresh=refresh)
    finally:
        # Cleanup must not mask whether an import transaction already committed.
        if cache is not None:
            try:
                await cache.close()
            except Exception:
                pass
        if redis_client is not None:
            try:
                await redis_client.aclose()
            except Exception:
                pass
        try:
            await repository.close()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description='Import a one-time JSON snapshot into local MySQL. Defaults to dry-run; migrate SQL schema first.')
    parser.add_argument('--input', required=True, type=Path, help='Complete exported JSON snapshot.')
    parser.add_argument('--apply', action='store_true', help='Commit changes, then refresh the local Redis cache.')
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.input, args.apply))
    except TransferError as error:
        print(json.dumps({'ok': False, 'persisted': error.persisted, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print('{"ok": false, "persisted": false, "error": "Import setup failed"}', file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, 'dry_run': not args.apply, **result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
