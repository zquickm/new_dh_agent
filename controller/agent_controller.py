from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from controller.streaming.sse_encoder import encode_chat_stream
from middleware.internal_request_auth import GatewayIdentity, get_current_user_identity
from schemas.chat import AgentRequest
from services.chat_service import chat_service
from services.conversation_service import conversation_service


router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/chat/stream")
async def stream_chat(
    request: AgentRequest,
    identity: GatewayIdentity = Depends(get_current_user_identity),
):
    # Check ownership before StreamingResponse commits an HTTP 200/SSE stream.
    if request.conversation_id:
        conversation = await conversation_service.get(
            request.conversation_id, user_id=identity.user_id
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
    return StreamingResponse(
        encode_chat_stream(chat_service.stream_chat(request, user_id=identity.user_id, identity=identity)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
