from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel


T = TypeVar("T")


class JsonResult(BaseModel, Generic[T]):
    code: int = 200
    message: str = "success"
    data: Optional[T] = None

    @classmethod
    def success(cls, data: Any = None) -> "JsonResult[Any]":
        return cls(data=data)

    @classmethod
    def error(cls, code: int, message: str) -> "JsonResult[Any]":
        return cls(code=code, message=message)
