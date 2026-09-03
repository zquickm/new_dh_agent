from __future__ import annotations

from hmac import compare_digest

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


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
