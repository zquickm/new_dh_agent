from datetime import datetime, timezone

from pydantic.alias_generators import to_camel

from database.runtime import database_runtime
from repositories.mysql_admin_conversation_repository import MySQLAdminConversationRepository
from schemas.admin_conversation import (
    AdminConversationDetail, AdminConversationPage, AdminEventPage, AdminRunDetail,
)


def database_datetime(value):
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def stored_record(item, *, exclude=()):
    """Keep JSON keys intact and expose persisted columns as camelCase metadata."""
    result = {}
    for column in item.__table__.columns:
        if column.key in exclude:
            continue
        value = getattr(item, column.key)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc).isoformat()
        if isinstance(value, int) and not isinstance(value, bool) and (column.key.endswith("_id") or column.key == "id"):
            value = str(value)
        result[to_camel(column.key)] = value
    return result


class AdminConversationService:
    def __init__(self, repository):
        self._repository = repository

    async def list_conversations(self, **params):
        params["start_at"] = database_datetime(params["start_at"])
        params["end_at"] = database_datetime(params["end_at"])
        params["keyword"] = (params["keyword"] or "").strip() or None
        if params["start_at"] is not None and params["end_at"] is not None and params["start_at"] > params["end_at"]:
            raise ValueError("开始时间不能晚于结束时间")
        items, total = await self._repository.list_conversations(**params)
        return AdminConversationPage(items=[stored_record(item, exclude=("map_context",)) for item in items],
                                     total=total, page=params["page"], page_size=params["page_size"])

    async def get_conversation(self, conversation_id, *, page, page_size):
        result = await self._repository.get_conversation(conversation_id, page=page, page_size=page_size)
        if result is None:
            return None
        conversation, messages, runs, total = result
        return AdminConversationDetail(conversation=stored_record(conversation),
            messages=[stored_record(item, exclude=("model_messages",)) for item in messages],
            runs=[stored_record(item) for item in runs], total=total, page=page, page_size=page_size)

    async def get_run(self, conversation_id, run_id, *, page, page_size):
        result = await self._repository.get_run(conversation_id, run_id, page=page, page_size=page_size)
        if result is None:
            return None
        run, messages, prompt, tools, total = result
        return AdminRunDetail(run=stored_record(run), model_messages=messages,
            system_prompt=stored_record(prompt) if prompt else None,
            tool_calls=[stored_record(item) for item in tools], total=total, page=page, page_size=page_size)

    async def get_events(self, conversation_id, run_id, *, after_sequence, page_size):
        result = await self._repository.get_events(conversation_id, run_id, after_sequence=after_sequence, page_size=page_size)
        if result is None:
            return None
        items, has_more = result
        return AdminEventPage(items=[stored_record(item) for item in items], has_more=has_more,
                              next_sequence=items[-1].sequence_no if items else after_sequence)


admin_conversation_service = AdminConversationService(MySQLAdminConversationRepository(database_runtime))
