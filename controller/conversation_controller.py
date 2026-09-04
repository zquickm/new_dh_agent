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
