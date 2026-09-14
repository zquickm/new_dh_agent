import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from common.json_result import JsonResult
from middleware.internal_request_auth import (
    GatewayIdentity,
    get_current_admin_id,
    get_current_user_id,
    get_current_user_identity,
)
from repositories.mysql_feedback_repository import InvalidFeedbackTarget
from schemas.feedback import SubmitFeedbackRequest
from services.feedback_service import feedback_service


logger = logging.getLogger(__name__)
user_router = APIRouter(tags=["feedback"])
admin_router = APIRouter(prefix="/admin/feedback", tags=["admin-feedback"])


@user_router.put(
    "/conversations/{conversation_id}/messages/{message_id}/feedback"
)
async def submit_feedback(
    conversation_id: str,
    message_id: str,
    request: SubmitFeedbackRequest,
    identity: GatewayIdentity = Depends(get_current_user_identity),
):
    try:
        result = await feedback_service.submit(
            conversation_id,
            message_id,
            request,
            user_id=identity.user_id,
            username=identity.username,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话消息不存在") from exc
    except InvalidFeedbackTarget as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JsonResult.success(result)


@user_router.delete(
    "/conversations/{conversation_id}/messages/{message_id}/feedback"
)
async def withdraw_feedback(
    conversation_id: str,
    message_id: str,
    user_id: str = Depends(get_current_user_id),
):
    try:
        result = await feedback_service.withdraw(
            conversation_id,
            message_id,
            user_id=user_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="会话消息不存在") from exc
    return JsonResult.success(result)


@admin_router.get("")
async def list_feedback(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    feedback_type: Optional[Literal["LIKE", "DISLIKE"]] = Query(
        default=None, alias="feedbackType"
    ),
    status: Optional[Literal["ACTIVE", "WITHDRAWN"]] = None,
    user_id: Optional[int] = Query(default=None, alias="userId", ge=1),
    keyword: Optional[str] = Query(default=None, max_length=100),
    start_at: Optional[datetime] = Query(default=None, alias="startAt"),
    end_at: Optional[datetime] = Query(default=None, alias="endAt"),
    _admin_id: int = Depends(get_current_admin_id),
):
    try:
        result = await feedback_service.list_admin(
            page=page,
            page_size=page_size,
            feedback_type=feedback_type,
            status=status,
            user_id=user_id,
            keyword=keyword,
            start_at=start_at,
            end_at=end_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JsonResult.success(result)


@admin_router.get("/{feedback_id}")
async def get_feedback_detail(
    feedback_id: str,
    before_turns: int = Query(default=3, alias="beforeTurns", ge=0, le=20),
    after_turns: int = Query(default=3, alias="afterTurns", ge=0, le=20),
    admin_id: int = Depends(get_current_admin_id),
):
    result = await feedback_service.get_admin_detail(
        feedback_id,
        before_turns=before_turns,
        after_turns=after_turns,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="反馈记录不存在")
    logger.info(
        "admin_feedback_detail_viewed admin_id=%s feedback_id=%s conversation_id=%s",
        admin_id,
        feedback_id,
        result.conversation_id,
    )
    return JsonResult.success(result)
