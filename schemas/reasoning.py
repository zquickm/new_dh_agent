"""Shared, versioned display snapshot for generated and fixed answers."""
from copy import deepcopy
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ReasoningRoundPresentation(BaseModel):
    model_config = ConfigDict(extra="allow")
    round_number: Annotated[StrictInt, Field(ge=1)]
    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=20000)
    min_duration_ms: Annotated[StrictInt, Field(ge=0, le=300000)] | None = None
    max_duration_ms: Annotated[StrictInt, Field(ge=0, le=300000)] | None = None

    @model_validator(mode="after")
    def duration_range(self):
        if (self.min_duration_ms is None) != (self.max_duration_ms is None):
            raise ValueError("Both thinking duration bounds are required")
        if self.min_duration_ms is not None and self.min_duration_ms > self.max_duration_ms:
            raise ValueError("Minimum thinking duration exceeds maximum")
        return self


class ReasoningPresentation(BaseModel):
    model_config = ConfigDict(extra="allow")
    rounds: list[ReasoningRoundPresentation] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_rounds(self):
        if len({item.round_number for item in self.rounds}) != len(self.rounds):
            raise ValueError("Presentation round numbers must be unique")
        return self


class ReasoningBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1)
    type: Literal["reasoning"]
    round_number: int = Field(ge=1)
    status: Literal["streaming", "completed", "failed", "cancelled"]
    content: str


class ProcessBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = Field(min_length=1)
    type: Literal["process"]
    round_number: int = Field(ge=1)
    data: dict


class ReasoningSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal[1] = 1
    blocks: list[Annotated[Union[ReasoningBlock, ProcessBlock], Field(discriminator="type")]] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({block.id for block in self.blocks}) != len(self.blocks):
            raise ValueError("Reasoning block IDs must be unique")
        # Validate optional metadata without adding fields to old snapshots.
        if self.model_extra.get("presentation") is not None:
            ReasoningPresentation.model_validate(self.model_extra["presentation"])
        return self


def empty_reasoning() -> dict:
    return {"schema_version": 1, "blocks": []}


def apply_reasoning_event(snapshot: dict, event: str, data: dict) -> dict:
    result = deepcopy(snapshot)
    if "presentation" in data:
        result["presentation"] = deepcopy(data["presentation"])
    if event == "process" and data.get("display_only"):
        return result
    blocks = result["blocks"]
    if event == "reasoning_delta":
        block_id = data["block_id"]
        block = next((b for b in blocks if b["id"] == block_id), None)
        if block is None:
            block = {"id": block_id, "type": "reasoning", "round_number": data["round_number"],
                     "status": "streaming", "content": ""}
            blocks.append(block)
        block["content"] += data["content"]
    elif event == "process":
        block_id = data["step_id"]
        block = {"id": block_id, "type": "process", "round_number": data.get("round_number", 1), "data": deepcopy(data)}
        index = next((i for i, b in enumerate(blocks) if b["id"] == block_id), None)
        if index is None:
            blocks.append(block)
        else:
            blocks[index] = block
    elif event == "reasoning_block":
        block = deepcopy(data["block"])
        index = next((i for i, b in enumerate(blocks) if b["id"] == block["id"]), None)
        if index is None:
            blocks.append(block)
        else:
            blocks[index] = block
    return result


def finish_reasoning(snapshot: dict, status: str = "completed") -> dict:
    result = deepcopy(snapshot)
    for block in result["blocks"]:
        if block["type"] == "reasoning" and block["status"] == "streaming":
            block["status"] = status
        elif block["type"] == "process" and block["data"].get("status") == "running":
            block["data"]["status"] = "cancelled" if status == "cancelled" else "failed"
    return result
