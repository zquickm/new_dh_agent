from __future__ import annotations

from typing import Any, Optional


class SensitiveWordError(RuntimeError):
    """A safe-to-display error; never include connection or input text here."""

    def __init__(self, message: str, status_code: int = 400, data: Optional[Any] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data
