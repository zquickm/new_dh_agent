from __future__ import annotations

from hmac import compare_digest
from typing import Optional

from fastapi import Depends, Header, HTTPException

from config.settings import get_settings


async def require_admin_token(
    x_dh_admin_token: Optional[str] = Header(default=None, alias="X-DH-Admin-Token"),
    settings=Depends(get_settings),
) -> None:
    """Require a separate admin credential after the application gateway check."""
    expected = settings.sensitive_words.admin_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=503, detail="敏感词管理未启用")
    actual = (x_dh_admin_token or "").encode("utf-8")
    if not compare_digest(actual, expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="敏感词管理认证失败")
