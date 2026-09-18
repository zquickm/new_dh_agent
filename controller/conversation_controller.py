import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from common.json_result import JsonResult
from middleware.internal_request_auth import GatewayIdentity, get_current_user_id, get_current_user_identity
from schemas.conversation import CreateConversationRequest
from services.conversation_service import conversation_service
from controller.streaming.sse_encoder import encode_sse, encode_sse_heartbeat


router = APIRouter(tags=["conversation"])


@router.get("/conversations")
async def list_conversations(
    limit: int = Query(default=20, ge=1, le=50),
    user_id: str = Depends(get_current_user_id),
):
    return JsonResult.success(await conversation_service.list(limit, user_id=user_id))


@router.post("/conversations")
async def create_conversation(
    request: CreateConversationRequest,
    identity: GatewayIdentity = Depends(get_current_user_identity),
):
    return JsonResult.success(await conversation_service.create(request, user_id=identity.user_id, identity=identity))


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user_id: str = Depends(get_current_user_id),
):
    item = await conversation_service.get(conversation_id, user_id=user_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JsonResult.success(item)


@router.get("/conversations/{conversation_id}/runs/{run_id}")
async def get_conversation_run(
    conversation_id: str,
    run_id: str,
    user_id: str = Depends(get_current_user_id),
):
    item = await conversation_service.get_run(
        conversation_id, run_id, user_id=user_id
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Conversation run not found")
    return JsonResult.success(item)


@router.get("/conversations/{conversation_id}/runs/{run_id}/events")
async def replay_conversation_run_events(
    conversation_id: str,
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    user_id: str = Depends(get_current_user_id),
):
    cursor = after
    if last_event_id not in (None, ""):
        try:
            parsed_last_event_id = int(last_event_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc
        if parsed_last_event_id < 0:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID")
        cursor = max(cursor, parsed_last_event_id)

    run = await conversation_service.get_run(
        conversation_id, run_id, user_id=user_id
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Conversation run not found")

    async def event_stream():
        nonlocal cursor
        idle_polls = 0
        terminal_idle_polls = 0
        terminal_statuses = {"completed", "failed", "interrupted", "rejected"}
        while True:
            events, status = await conversation_service.list_run_events(
                conversation_id,
                run_id,
                after=cursor,
                user_id=user_id,
            )
            if status is None:
                return
            if events:
                idle_polls = 0
                terminal_idle_polls = 0
                for event in events:
                    cursor = event.id
                    yield encode_sse(event.model_dump())
                continue
            if status in terminal_statuses:
                # Run finalization and the terminal event are adjacent commits.
                # Wait through one poll so a subscriber cannot miss done/error.
                terminal_idle_polls += 1
                if terminal_idle_polls >= 2:
                    return
            else:
                terminal_idle_polls = 0
            idle_polls += 1
            if idle_polls >= 15:
                idle_polls = 0
                yield encode_sse_heartbeat()
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations/{conversation_id}/runs/{run_id}/tool-calls")
async def list_run_tool_calls(
    conversation_id: str,
    run_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    user_id: str = Depends(get_current_user_id),
):
    run = await conversation_service.get_run(
        conversation_id, run_id, user_id=user_id
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Conversation run not found")
    return JsonResult.success(
        await conversation_service.list_tool_calls(
            conversation_id,
            run_id,
            user_id=user_id,
            limit=limit,
        )
    )


@router.get(
    "/conversations/{conversation_id}/runs/{run_id}/tool-calls/{tool_call_id}"
)
async def get_run_tool_call(
    conversation_id: str,
    run_id: str,
    tool_call_id: str,
    user_id: str = Depends(get_current_user_id),
):
    item = await conversation_service.get_tool_call(
        conversation_id,
        run_id,
        tool_call_id,
        user_id=user_id,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Tool call not found")
    return JsonResult.success(item)
