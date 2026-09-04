#!/usr/bin/env python3
"""One-time read-only export from a deployed source API."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.sensitive_word_transfer import TransferError, export_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description='Export the complete source agent dictionary once; never writes to the source.')
    parser.add_argument('--output', required=True, type=Path, help='Destination JSON snapshot (atomically replaced on success).')
    parser.add_argument('--source-system', default='hk-ai-tutor', help='Stable source identity used for idempotent local imports.')
    args = parser.parse_args()
    try:
        snapshot = asyncio.run(export_snapshot(os.getenv('SOURCE_SENSITIVE_BASE_URL', ''), os.getenv('SOURCE_SENSITIVE_TOKEN', ''), args.source_system, args.output))
    except TransferError as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print('{"ok": false, "error": "Export failed"}', file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, 'categories': len(snapshot['categories']), 'words': len(snapshot['words'])}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
