from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query

from common.json_result import JsonResult
from common.sensitive_errors import SensitiveWordError
from middleware.admin_auth import require_admin_token
from schemas.sensitive_word import (
    CategoryCreateRequest,
    CategoryUpdateRequest,
    SensitiveCheckRequest,
    WordCreateRequest,
    WordImportRequest,
    WordUpdateRequest,
)
from services.sensitive_word_admin_service import SensitiveWordAdminService


def require_agent_scope(scope: str) -> None:
    if scope != "agent":
        raise SensitiveWordError("当前仅支持 agent 作用域", status_code=400)


def get_admin_service() -> SensitiveWordAdminService:
    # Resolve the lifespan-owned runtime only when an authenticated route is used.
    from services.sensitive_word_runtime import get_sensitive_word_runtime

    return SensitiveWordAdminService(get_sensitive_word_runtime())


router = APIRouter(
    prefix="/internal/sensitive/{scope}",
    tags=["sensitive-word-admin"],
    dependencies=[Depends(require_admin_token), Depends(require_agent_scope)],
)


@router.get("/categories")
async def list_categories(scope: str, service=Depends(get_admin_service)):
    return JsonResult.success(await service.list_categories(scope))


@router.post("/categories")
async def create_category(scope: str, request: CategoryCreateRequest, service=Depends(get_admin_service)):
    return JsonResult.success(await service.mutate(scope, "create_category", request.model_dump()))


@router.patch("/categories/{category_id}")
async def update_category(scope: str, request: CategoryUpdateRequest, category_id: int = Path(gt=0), service=Depends(get_admin_service)):
    return JsonResult.success(await service.mutate(scope, "update_category", category_id, request.model_dump(exclude_unset=True)))


@router.delete("/categories/{category_id}")
async def delete_category(scope: str, category_id: int = Path(gt=0), service=Depends(get_admin_service)):
    return JsonResult.success(await service.mutate(scope, "delete_category", category_id))


@router.get("/words")
async def list_words(
    scope: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    word: Optional[str] = Query(default=None, max_length=255),
    category_id: Optional[int] = Query(default=None, gt=0),
    status: Optional[int] = Query(default=None, ge=0, le=1),
    service=Depends(get_admin_service),
):
    return JsonResult.success(await service.list_words(scope, page=page, page_size=page_size, word=word, category_id=category_id, status=status))


@router.post("/words")
async def create_word(scope: str, request: WordCreateRequest, service=Depends(get_admin_service)):
    return JsonResult.success(await service.mutate(scope, "create_word", request.model_dump()))


@router.patch("/words/{word_id}")
async def update_word(scope: str, request: WordUpdateRequest, word_id: int = Path(gt=0), service=Depends(get_admin_service)):
    return JsonResult.success(await service.mutate(scope, "update_word", word_id, request.model_dump(exclude_unset=True)))


@router.delete("/words/{word_id}")
async def delete_word(scope: str, word_id: int = Path(gt=0), service=Depends(get_admin_service)):
    return JsonResult.success(await service.mutate(scope, "delete_word", word_id))


@router.post("/words/import")
async def import_words(scope: str, request: WordImportRequest, service=Depends(get_admin_service)):
    return JsonResult.success(await service.import_words(scope, **request.model_dump()))


@router.post("/check")
async def check(scope: str, request: SensitiveCheckRequest, service=Depends(get_admin_service)):
    return JsonResult.success(await service.check(scope, request.text))


@router.post("/refresh")
async def refresh(scope: str, service=Depends(get_admin_service)):
    return JsonResult.success(await service.refresh(scope))
