from dataclasses import dataclass
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class AgentEvent:
    type: Literal[
        "answer_delta",
        "retrieval",
        "tool_started",
        "tool_finished",
    ]
    content: str = ""
    data: Optional[dict[str, Any]] = None
    sources: Optional[list[dict[str, Any]]] = None
    round_number: int = 0
    tool_name: str = ""
    tool_call_id: str = ""
    success: bool = True
