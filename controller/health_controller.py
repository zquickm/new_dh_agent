from fastapi import APIRouter

from common.json_result import JsonResult
from config.settings import get_settings


router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    settings = get_settings()
    return JsonResult.success(
        {
            "status": "ok",
            "profile": settings.app_profile,
            "model": settings.llm.model,
            "stream_endpoint": "/agent/chat/stream",
        }
    )
