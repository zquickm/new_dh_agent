from fastapi import APIRouter, HTTPException, Query

from common.json_result import JsonResult
from schemas.conversation import CreateConversationRequest
from services.conversation_service import conversation_service


router = APIRouter(tags=["conversation"])


@router.get("/conversations")
async def list_conversations(limit: int = Query(default=20, ge=1, le=50)):
    return JsonResult.success(await conversation_service.list(limit))


@router.post("/conversations")
async def create_conversation(request: CreateConversationRequest):
    return JsonResult.success(await conversation_service.create(request))


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    item = await conversation_service.get(conversation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return JsonResult.success(item)
