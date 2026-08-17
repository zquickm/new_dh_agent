from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AgentEvent:
    type: Literal[
        "answer_delta",
        "tool_started",
        "tool_finished",
    ]
    content: str = ""
    round_number: int = 0
    tool_name: str = ""
    tool_call_id: str = ""
    success: bool = True
