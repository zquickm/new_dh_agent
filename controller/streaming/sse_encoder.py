import json
from collections.abc import AsyncIterator

from common.json_result import JsonResult


def encode_sse(event: dict) -> str:
    payload = JsonResult.success(event.get("data", {})).model_dump()
    event_id = event.get("id")
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return (
        f"event: {event['event']}\n"
        f"{id_line}"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def encode_sse_heartbeat() -> str:
    return ": heartbeat\n\n"


async def encode_chat_stream(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async for event in events:
        yield encode_sse(event)
