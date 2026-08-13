from fastapi import APIRouter, Query

from common.json_result import JsonResult


router = APIRouter(tags=["recommendation"])


@router.get("/recommend-questions")
async def recommend_questions(limit: int = Query(default=6, ge=1, le=20)):
    del limit
    return JsonResult.success([])
