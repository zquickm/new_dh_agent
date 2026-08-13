import json
from collections.abc import AsyncIterator

from common.json_result import JsonResult


def encode_sse(event: dict) -> str:
    payload = JsonResult.success(event.get("data", {})).model_dump()
    return (
        f"event: {event['event']}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


async def encode_chat_stream(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async for event in events:
        yield encode_sse(event)
