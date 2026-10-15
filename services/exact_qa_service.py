from typing import Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config.settings import get_settings
from schemas.reasoning import ReasoningSnapshot


class ExactQaSnapshot(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    qa_id: int = Field(alias="qaId", gt=0)
    version_id: int = Field(alias="versionId", gt=0)
    version_no: int = Field(alias="versionNo", gt=0)
    question: str
    answer: str = Field(min_length=1)
    reasoning: ReasoningSnapshot
    sources: list[dict]
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def finished_blocks(self):
        for block in self.reasoning.blocks:
            if block.type == "reasoning" and block.status == "streaming":
                raise ValueError("Fixed answers must contain finished reasoning")
        rounds = {block.round_number for block in self.reasoning.blocks if block.type == "reasoning"}
        presentation = self.reasoning.model_extra.get("presentation")
        if presentation is not None:
            if any(item["round_number"] not in rounds for item in presentation.get("rounds", [])):
                raise ValueError("Presentation requires a corresponding reasoning round")
        return self

    def provenance(self) -> dict:
        return {"qa_id": self.qa_id, "version_id": self.version_id,
                "version_no": self.version_no, "checksum": self.checksum}


class ExactQaService:
    async def match(self, question: str) -> Optional[ExactQaSnapshot]:
        settings = get_settings()
        if not settings.exact_qa.enabled or len(question) > 20000:
            return None
        # The internal read endpoint is never exposed as a separate chat route.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.exact_qa.timeout, connect=settings.dictionaries.connect_timeout),
            verify=settings.dictionaries.verify_ssl,
        ) as client:
            response = await client.post(
                settings.dictionaries.base_url.rstrip("/") + "/internal/v1/qa/exact-match",
                headers={settings.internal_request.header_name: settings.internal_request.token.get_secret_value()},
                json={"question": question},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError("Exact QA service rejected the request")
        if payload.get("data") is None:
            return None
        snapshot = ExactQaSnapshot.model_validate(payload["data"])
        if snapshot.question != question:
            raise RuntimeError("Exact QA service returned a different question")
        return snapshot
