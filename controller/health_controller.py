from fastapi import APIRouter, HTTPException

from common.json_result import JsonResult
from config.settings import get_settings
from database.runtime import database_runtime


router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    settings = get_settings()
    if not await database_runtime.ping():
        raise HTTPException(status_code=503, detail="数据库连接不可用")
    return JsonResult.success(
        {
            "status": "ok",
            "profile": settings.app_profile,
            "model": settings.llm.model,
            "stream_endpoint": "/agent/chat/stream",
            "persistence": "mysql" if settings.persistence.enabled else "memory",
        }
    )
