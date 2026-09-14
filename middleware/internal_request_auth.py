from __future__ import annotations

import re
from typing import NamedTuple
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


class GatewayIdentity(NamedTuple):
    user_id: str
    username: str


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
    return GatewayIdentity(user_id=user_id, username=username)


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
