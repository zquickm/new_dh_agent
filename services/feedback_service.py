from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from repositories.repository import feedback_repository
from schemas.feedback import SubmitFeedbackRequest


def _database_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


class FeedbackService:
    async def submit(
        self,
        conversation_id: str,
        message_id: str,
        request: SubmitFeedbackRequest,
        *,
        user_id: str,
        username: str,
    ):
        return await feedback_repository.submit(
            conversation_id,
            message_id,
            feedback_type=request.feedback_type,
            comment=request.comment,
            user_id=user_id,
            username=username,
        )

    async def withdraw(
        self,
        conversation_id: str,
        message_id: str,
        *,
        user_id: str,
    ):
        return await feedback_repository.withdraw(
            conversation_id,
            message_id,
            user_id=user_id,
        )

    async def list_admin(
        self,
        *,
        page: int,
        page_size: int,
        feedback_type: Optional[str],
        status: Optional[str],
        user_id: Optional[int],
        keyword: Optional[str],
        start_at: Optional[datetime],
        end_at: Optional[datetime],
    ):
        normalized_keyword = keyword.strip() if keyword else None
        database_start_at = _database_datetime(start_at)
        database_end_at = _database_datetime(end_at)
        if (
            database_start_at is not None
            and database_end_at is not None
            and database_start_at > database_end_at
        ):
            raise ValueError("开始时间不能晚于结束时间")
        return await feedback_repository.list_admin(
            page=page,
            page_size=page_size,
            feedback_type=feedback_type,
            status=status,
            user_id=user_id,
            keyword=normalized_keyword,
            start_at=database_start_at,
            end_at=database_end_at,
        )

    async def get_admin_detail(
        self,
        feedback_id: str,
        *,
        before_turns: int,
        after_turns: int,
    ):
        return await feedback_repository.get_admin_detail(
            feedback_id,
            before_turns=before_turns,
            after_turns=after_turns,
        )


feedback_service = FeedbackService()
