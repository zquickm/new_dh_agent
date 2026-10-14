import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from common.json_result import JsonResult
from middleware.internal_request_auth import get_current_admin_id
from services.admin_conversation_service import admin_conversation_service as service


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/conversations", tags=["admin-conversations"])


def response(result, *, admin_id, conversation_id, run_id=None):
    if result is None:
        raise HTTPException(status_code=404, detail="会话或运行记录不存在或已删除")
    logger.info("admin_conversation_debug_viewed admin_id=%s conversation_id=%s run_id=%s", admin_id, conversation_id, run_id)
    return JsonResult.success(result.model_dump(by_alias=True))


@router.get("")
async def list_conversations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    user_id: Optional[int] = Query(default=None, alias="userId", ge=1, le=9223372036854775807),
    keyword: Optional[str] = Query(default=None, max_length=100),
    start_at: Optional[datetime] = Query(default=None, alias="startAt"),
    end_at: Optional[datetime] = Query(default=None, alias="endAt"),
    time_field: Literal["createdAt", "updatedAt"] = Query(default="updatedAt", alias="timeField"),
    sort_by: Literal["createdAt", "updatedAt", "userId"] = Query(default="updatedAt", alias="sortBy"),
    sort_order: Literal["asc", "desc"] = Query(default="desc", alias="sortOrder"),
    _admin_id: int = Depends(get_current_admin_id),
):
    try:
        result = await service.list_conversations(page=page, page_size=page_size, user_id=user_id,
            keyword=keyword, start_at=start_at, end_at=end_at, time_field=time_field,
            sort_by=sort_by, sort_order=sort_order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JsonResult.success(result.model_dump(by_alias=True))


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    admin_id: int = Depends(get_current_admin_id),
):
    result = await service.get_conversation(conversation_id, page=page, page_size=page_size)
    return response(result, admin_id=admin_id, conversation_id=conversation_id)


@router.get("/{conversation_id}/runs/{run_id}")
async def get_run(
    conversation_id: str, run_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, alias="pageSize", ge=1, le=100),
    admin_id: int = Depends(get_current_admin_id),
):
    result = await service.get_run(conversation_id, run_id, page=page, page_size=page_size)
    return response(result, admin_id=admin_id, conversation_id=conversation_id, run_id=run_id)


@router.get("/{conversation_id}/runs/{run_id}/events")
async def get_events(
    conversation_id: str, run_id: str,
    after_sequence: int = Query(default=0, alias="afterSequence", ge=0),
    page_size: int = Query(default=100, alias="pageSize", ge=1, le=200),
    admin_id: int = Depends(get_current_admin_id),
):
    result = await service.get_events(conversation_id, run_id, after_sequence=after_sequence, page_size=page_size)
    return response(result, admin_id=admin_id, conversation_id=conversation_id, run_id=run_id)
