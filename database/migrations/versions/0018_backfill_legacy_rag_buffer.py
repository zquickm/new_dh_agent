"""backfill legacy cited RAG sources into conversation buffers

Revision ID: 0018
Revises: 0017
"""
import hashlib
import json
from typing import Any, Sequence, Union

from alembic import context, op
import sqlalchemy as sa


revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _optional_int(value: Any):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_string(value: Any):
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _score(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _identity(source: dict[str, Any], text: str) -> str:
    value = {
        "knowledge_id": _optional_string(source.get("knowledge_id")),
        "document_id": _optional_string(source.get("document_id")),
        "chunk_id": _optional_string(source.get("chunk_id")),
        "source": str(source.get("source") or "").strip().casefold(),
        "page_index": _optional_int(source.get("page_index")),
        "line": _optional_int(source.get("line")),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def upgrade() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    rows = bind.execute(sa.text("""
        SELECT `id`, `conversation_id`, `user_id`, `sources`, `run_id`, `created_at`
        FROM `agent_conversation_message`
        WHERE `role` = 'assistant' AND JSON_LENGTH(`sources`) > 0
        ORDER BY `conversation_id`, `sequence_no`
    """)).mappings().all()
    for row in rows:
        sources = row["sources"]
        if isinstance(sources, str):
            sources = json.loads(sources)
        if not isinstance(sources, list):
            continue
        changed = False
        for source in sources:
            if not isinstance(source, dict) or source.get("source_type") == "database":
                continue
            if source.get("buffer_id") is not None:
                continue
            text = str(source.get("preview") or "").strip()
            source_name = str(source.get("source") or "").strip()
            if not text or not source_name:
                continue
            identity_hash = _identity(source, text)
            bind.execute(sa.text("""
                INSERT INTO `agent_conversation_rag_buffer` (
                    `conversation_id`, `user_id`, `identity_hash`, `knowledge_id`,
                    `document_id`, `chunk_id`, `source`, `title`, `text`, `url`,
                    `page_index`, `line`, `page`, `score`, `first_query`,
                    `created_run_id`, `created_at`, `last_seen_at`
                ) VALUES (
                    :conversation_id, :user_id, :identity_hash, :knowledge_id,
                    :document_id, :chunk_id, :source, :title, :text, :url,
                    :page_index, :line, :page, :score, NULL,
                    :created_run_id, :created_at, :created_at
                )
                ON DUPLICATE KEY UPDATE `last_seen_at` = `last_seen_at`
            """), {
                "conversation_id": row["conversation_id"],
                "user_id": row["user_id"],
                "identity_hash": identity_hash,
                "knowledge_id": _optional_string(source.get("knowledge_id")),
                "document_id": _optional_string(source.get("document_id")),
                "chunk_id": _optional_string(source.get("chunk_id")),
                "source": source_name,
                "title": source.get("title"),
                "text": text,
                "url": source.get("url"),
                "page_index": _optional_int(source.get("page_index")),
                "line": _optional_int(source.get("line")),
                "page": _optional_int(source.get("page")),
                "score": _score(source.get("score")),
                "created_run_id": row["run_id"],
                "created_at": row["created_at"],
            })
            buffer_id = bind.execute(sa.text("""
                SELECT `id` FROM `agent_conversation_rag_buffer`
                WHERE `conversation_id` = :conversation_id
                  AND `user_id` = :user_id
                  AND `identity_hash` = :identity_hash
            """), {
                "conversation_id": row["conversation_id"],
                "user_id": row["user_id"],
                "identity_hash": identity_hash,
            }).scalar_one()
            source["buffer_id"] = int(buffer_id)
            changed = True
        if changed:
            bind.execute(sa.text("""
                UPDATE `agent_conversation_message`
                SET `sources` = CAST(:sources AS JSON)
                WHERE `id` = :message_id
            """), {
                "sources": json.dumps(sources, ensure_ascii=False),
                "message_id": row["id"],
            })


def downgrade() -> None:
    # Historical answers remain valid with an extra ignored buffer_id field.
    pass
