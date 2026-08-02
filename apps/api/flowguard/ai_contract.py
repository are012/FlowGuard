"""Pure backend-to-AI interpretation contract with no service dependencies."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from flowguard.ai_numeric_policy import contains_numeric_expression


class AIContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


AIIdentifier = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
]


class LabelEntityKind(StrEnum):
    CLIENT = "CLIENT"
    MERCHANT = "MERCHANT"
    PLATFORM = "PLATFORM"
    CARD_PAYMENT = "CARD_PAYMENT"
    OTHER = "OTHER"


class LabelCategoryHint(StrEnum):
    RECEIVABLE = "RECEIVABLE"
    CARD_BILL = "CARD_BILL"
    INSTALLMENT_PAYMENT = "INSTALLMENT_PAYMENT"
    RENT = "RENT"
    INSURANCE = "INSURANCE"
    UTILITY = "UTILITY"
    LOAN_PAYMENT = "LOAN_PAYMENT"
    TAX = "TAX"
    SAVINGS = "SAVINGS"
    DISCRETIONARY_EXPENSE = "DISCRETIONARY_EXPENSE"
    OTHER_INFLOW = "OTHER_INFLOW"
    OTHER_OUTFLOW = "OTHER_OUTFLOW"


class LabelClassificationConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


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


class LabelClassificationEnvelope(AIContractModel):
    schemaVersion: Literal["1.2"]
    contractVersion: Literal["1.2"]
    promptVersion: Literal["classify-1"]
    requestId: AIIdentifier
    idempotencyKey: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)
    ]
    importId: AIIdentifier
    locale: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=35)]


class LabelClassificationItem(AIContractModel):
    labelId: AIIdentifier
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    direction: Literal["INFLOW", "OUTFLOW"]
    occurrences: Annotated[int, Field(strict=True, ge=1)]

    @field_validator("text")
    @classmethod
    def text_has_no_amount_or_date_number(cls, value: str) -> str:
        if contains_numeric_expression(value):
            raise ValueError("classification label text must redact amount and date numbers")
        return value


class LabelClassificationRequest(LabelClassificationEnvelope):
    labels: list[LabelClassificationItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def label_ids_are_unique(self) -> LabelClassificationRequest:
        label_ids = [item.labelId for item in self.labels]
        if len(label_ids) != len(set(label_ids)):
            raise ValueError("labels labelId values must be unique")
        return self


class LabelClassificationGroup(AIContractModel):
    groupId: AIIdentifier
    labelIds: list[AIIdentifier] = Field(min_length=1)
    normalizedName: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ]
    entityKind: LabelEntityKind
    categoryHint: LabelCategoryHint
    essentialHint: bool | None
    confidence: LabelClassificationConfidence
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class LabelClassificationResponse(LabelClassificationEnvelope):
    groups: list[LabelClassificationGroup] = Field(max_length=500)
    ungrouped: list[AIIdentifier] = Field(max_length=500)

    @model_validator(mode="after")
    def group_ids_are_unique(self) -> LabelClassificationResponse:
        group_ids = [item.groupId for item in self.groups]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("groups groupId values must be unique")
        return self


__all__ = [
    "AIActionCandidatePayload",
    "AIActionCandidateRef",
    "AIToBackendResponse",
    "BackendToAIRequest",
    "LabelCategoryHint",
    "LabelClassificationConfidence",
    "LabelClassificationEnvelope",
    "LabelClassificationGroup",
    "LabelClassificationItem",
    "LabelClassificationRequest",
    "LabelClassificationResponse",
    "LabelEntityKind",
]
