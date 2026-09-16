# 提供管理员查询与更新 Skill 开关的接口，保持现有网关鉴权。
from fastapi import APIRouter, Depends, HTTPException

from common.json_result import JsonResult
from middleware.internal_request_auth import get_current_admin_id
from repositories.skill_repository import SkillNotFound, SkillRevisionConflict
from schemas.skill import UpdateSkillRequest
from services.skill_service import skill_service


router = APIRouter(prefix="/admin/skills", tags=["skills"])


@router.get("")
async def list_skills(_operator_id: int = Depends(get_current_admin_id)):
    return JsonResult.success(await skill_service.list_skills())


@router.put("/{skill_key}/enabled")
async def update_skill(
    skill_key: str,
    request: UpdateSkillRequest,
    operator_id: int = Depends(get_current_admin_id),
):
    try:
        result = await skill_service.update(
            skill_key,
            request.enabled,
            expected_revision=request.expected_revision,
            operator_id=operator_id,
        )
    except SkillNotFound as exc:
        raise HTTPException(status_code=404, detail="Skill 不存在") from exc
    except SkillRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JsonResult.success(result)
