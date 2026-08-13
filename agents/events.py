from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ReActEvent:
    type: Literal[
        "think",
        "think_finished",
        "action_started",
        "action_finished",
        "answer",
    ]
    content: str = ""
    round_number: int = 0
    tool_name: str = ""
    success: bool = True


@dataclass(frozen=True)
class ActionResult:
    tool_name: str
    tool_input: str
    observation: str
    success: bool
