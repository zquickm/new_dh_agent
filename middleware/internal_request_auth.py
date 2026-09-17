from __future__ import annotations

import re
from dataclasses import dataclass
from hmac import compare_digest
from urllib.parse import unquote

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def get_current_user_id(request: Request) -> str:
    """Resolve gateway identity after internal-token middleware validation.

    These headers do not replace gateway JWT validation or network isolation.
    """
    user_ids = request.headers.getlist("x-user-id")
    sources = request.headers.getlist("x-auth-source")
    if (
        len(user_ids) != 1
        or sources != ["dh-gateway"]
        or re.fullmatch(r"[1-9][0-9]{0,18}", user_ids[0]) is None
        or int(user_ids[0]) > 9223372036854775807
    ):
        raise HTTPException(status_code=401, detail="缺少或无效的网关用户身份")
    # Stable primary key, not the editable username or per-login token id.
    return user_ids[0]


@dataclass(frozen=True)
class GatewayIdentity:
    user_id: str
    username: str
    display_name: str = ""
    user_type: str = "USER"
    unit_id: str = "1"
    unit_code: str = "UNASSIGNED"
    unit_name: str = "待分配单位"
    department_id: str = "1"
    department_code: str = "UNASSIGNED"
    department_name: str = "待分配部门"
    role_codes: tuple[str, ...] = ("GENERAL_USER",)
    profile_version: int = 1


def get_current_user_identity(request: Request) -> GatewayIdentity:
    user_id = get_current_user_id(request)
    usernames = request.headers.getlist("x-username")
    if len(usernames) != 1:
        raise HTTPException(status_code=401, detail="缺少或无效的网关用户名")
    try:
        username = unquote(usernames[0], encoding="utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise HTTPException(status_code=401, detail="缺少或无效的网关用户名") from exc
    if (
        not username
        or len(username) > 64
        or any(ord(character) < 32 for character in username)
    ):
        raise HTTPException(status_code=401, detail="缺少或无效的网关用户名")
    return GatewayIdentity(
        user_id=user_id,
        username=username,
        display_name=_decoded_header(request, "x-display-name", "", 100),
        user_type=_token_header(request, "x-user-type", "USER", r"USER|ADMIN"),
        unit_id=_positive_id_header(request, "x-unit-id", "1"),
        unit_code=_token_header(request, "x-unit-code", "UNASSIGNED"),
        unit_name=_decoded_header(request, "x-unit-name", "待分配单位", 128),
        department_id=_positive_id_header(request, "x-department-id", "1"),
        department_code=_token_header(request, "x-department-code", "UNASSIGNED"),
        department_name=_decoded_header(request, "x-department-name", "待分配部门", 128),
        role_codes=_role_codes(request),
        profile_version=int(_positive_id_header(request, "x-profile-version", "1")),
    )


def _single_header(request: Request, name: str, fallback: str) -> str:
    values = request.headers.getlist(name)
    if not values:
        return fallback
    if len(values) != 1:
        raise HTTPException(status_code=401, detail="网关组织身份无效")
    return values[0]


def _decoded_header(request: Request, name: str, fallback: str, maximum: int) -> str:
    try:
        value = unquote(_single_header(request, name, fallback), encoding="utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise HTTPException(status_code=401, detail="网关组织身份无效") from exc
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise HTTPException(status_code=401, detail="网关组织身份无效")
    return value


def _positive_id_header(request: Request, name: str, fallback: str) -> str:
    value = _single_header(request, name, fallback)
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None or int(value) > 9223372036854775807:
        raise HTTPException(status_code=401, detail="网关组织身份无效")
    return value


def _token_header(request: Request, name: str, fallback: str, pattern: str = r"[A-Za-z0-9_.-]{2,64}") -> str:
    value = _single_header(request, name, fallback)
    if re.fullmatch(pattern, value) is None:
        raise HTTPException(status_code=401, detail="网关组织身份无效")
    return value


def _role_codes(request: Request) -> tuple[str, ...]:
    raw = _single_header(request, "x-role-codes", "GENERAL_USER")
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    if not values or len(values) > 32 or any(re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", value) is None for value in values):
        raise HTTPException(status_code=401, detail="网关组织身份无效")
    return values


def get_current_admin_id(request: Request) -> int:
    user_id = get_current_user_id(request)
    user_types = request.headers.getlist("x-user-type")
    if user_types != ["ADMIN"]:
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return int(user_id)


class InternalRequestAuthMiddleware:
    """Reject requests that were not forwarded by the trusted gateway path."""

    def __init__(self, app: ASGIApp, *, header_name: str, token: str) -> None:
        if not header_name.strip():
            raise ValueError("internal request header name must not be blank")
        if not token:
            raise ValueError("internal request token must not be blank")
        self._app = app
        self._header_name = header_name.lower().encode("ascii")
        self._expected_token = token.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        actual_token = self._find_header(scope)
        if actual_token is not None and compare_digest(
            actual_token,
            self._expected_token,
        ):
            await self._app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=403,
            content={
                "data": None,
                "message": "禁止绕过Gateway直接访问服务",
                "code": 403,
            },
        )
        await response(scope, receive, send)

    def _find_header(self, scope: Scope) -> bytes | None:
        for name, value in scope.get("headers", []):
            if name.lower() == self._header_name:
                return value
        return None
