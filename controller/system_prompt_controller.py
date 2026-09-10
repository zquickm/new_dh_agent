from fastapi import APIRouter, Depends, HTTPException, Query

from common.json_result import JsonResult
from middleware.internal_request_auth import get_current_admin_id
from repositories.system_prompt_repository import (
    PromptContentUnchanged,
    PromptRevisionConflict,
    PromptVersionNotFound,
)
from schemas.system_prompt import (
    PublishSystemPromptRequest,
    RollbackSystemPromptRequest,
    SaveSystemPromptDraftRequest,
)
from services.system_prompt_service import system_prompt_service


router = APIRouter(prefix="/admin/system-prompts", tags=["system-prompts"])


@router.get("/main")
async def get_main_prompt(_operator_id: int = Depends(get_current_admin_id)):
    return JsonResult.success(await system_prompt_service.get())


@router.put("/main/draft")
async def save_main_prompt_draft(
    request: SaveSystemPromptDraftRequest,
    operator_id: int = Depends(get_current_admin_id),
):
    try:
        result = await system_prompt_service.save_draft(
            request.content,
            expected_revision=request.expected_revision,
            operator_id=operator_id,
        )
    except PromptRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JsonResult.success(result)


@router.post("/main/publish")
async def publish_main_prompt(
    request: PublishSystemPromptRequest,
    operator_id: int = Depends(get_current_admin_id),
):
    try:
        result = await system_prompt_service.publish(
            expected_revision=request.expected_revision,
            release_note=request.release_note,
            operator_id=operator_id,
        )
    except (PromptRevisionConflict, PromptContentUnchanged) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JsonResult.success(result)


@router.get("/main/versions")
async def list_main_prompt_versions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, alias="pageSize", ge=1, le=100),
    _operator_id: int = Depends(get_current_admin_id),
):
    return JsonResult.success(await system_prompt_service.list_versions(page, page_size))


@router.post("/main/versions/{version_no}/rollback")
async def rollback_main_prompt(
    version_no: int,
    request: RollbackSystemPromptRequest,
    operator_id: int = Depends(get_current_admin_id),
):
    try:
        result = await system_prompt_service.rollback(
            version_no,
            expected_revision=request.expected_revision,
            release_note=request.release_note,
            operator_id=operator_id,
        )
    except PromptRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PromptVersionNotFound as exc:
        raise HTTPException(status_code=404, detail="提示词版本不存在") from exc
    return JsonResult.success(result)
