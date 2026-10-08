"""Progress transport supplied by the server."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass(frozen=True)
class ToolContext:
    on_progress: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None

    async def report(self, **progress: Any) -> None:
        if self.on_progress is not None:
            await self.on_progress(progress)
