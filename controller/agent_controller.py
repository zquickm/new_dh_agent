from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from controller.streaming.sse_encoder import encode_chat_stream
from schemas.chat import AgentRequest
from services.chat_service import chat_service


router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/chat/stream")
async def stream_chat(request: AgentRequest):
    return StreamingResponse(
        encode_chat_stream(chat_service.stream_chat(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
