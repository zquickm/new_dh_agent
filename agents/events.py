from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ReActEvent:
    type: Literal[
        "think",
        "think_finished",
        "answer",
    ]
    content: str = ""
    round_number: int = 0
