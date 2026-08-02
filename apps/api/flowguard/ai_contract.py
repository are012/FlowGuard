"""Pure backend-to-AI interpretation contract with no service dependencies."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


class AIContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


AIIdentifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class AIActionCandidateRef(AIContractModel):
    actionId: AIIdentifier
    priority: Annotated[int, Field(strict=True, ge=1, le=10)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class AIActionCandidatePayload(AIContractModel):
    actionId: AIIdentifier
    type: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
    ] = None
    amount: int | None = None
    feasible: bool | None = None


class NextRiskPayload(AIContractModel):
    type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    date: date
    shortageAmount: int


class CashflowSummaryPayload(AIContractModel):
    lowestBalance: int
    lowestBalanceDate: date


class FactsPayload(AIContractModel):
    safeToSpend: int
    nextRisk: NextRiskPayload
    cashflowSummary: CashflowSummaryPayload


class AIContractEnvelope(AIContractModel):
    schemaVersion: Literal["1.1"]
    contractVersion: Literal["1.1"]
    promptVersion: AIIdentifier
    requestId: AIIdentifier
    idempotencyKey: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
    ]
    analysisId: AIIdentifier
    snapshotId: AIIdentifier
    snapshotRevision: AIIdentifier
    locale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=35)]


class AIToBackendResponse(AIContractEnvelope):
    riskExplanation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    rankedActions: list[AIActionCandidateRef] = Field(default_factory=list)
    userMessage: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]

    @model_validator(mode="after")
    def ranked_actions_are_unique(self) -> AIToBackendResponse:
        action_ids = [item.actionId for item in self.rankedActions]
        priorities = [item.priority for item in self.rankedActions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("rankedActions actionId values must be unique")
        if len(priorities) != len(set(priorities)):
            raise ValueError("rankedActions priority values must be unique")
        return self


class BackendToAIRequest(AIContractEnvelope):
    calculatedAt: AwareDatetime
    facts: FactsPayload
    evidence: list[dict] = Field(default_factory=list)
    actionCandidates: list[AIActionCandidatePayload] = Field(default_factory=list)


__all__ = [
    "AIActionCandidatePayload",
    "AIActionCandidateRef",
    "AIToBackendResponse",
    "BackendToAIRequest",
]
