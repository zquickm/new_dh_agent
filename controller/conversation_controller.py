from fastapi import APIRouter, Depends, HTTPException, Query

from common.json_result import JsonResult
from middleware.internal_request_auth import get_current_user_id
from schemas.conversation import CreateConversationRequest
from services.conversation_service import conversation_service


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
    user_id: str = Depends(get_current_user_id),
):
    return JsonResult.success(await conversation_service.create(request, user_id=user_id))


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
