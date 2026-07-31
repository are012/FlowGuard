"""Strict REST request schemas."""

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


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AIActionCandidateRef(StrictRequest):
    actionId: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    priority: Annotated[int, Field(strict=True, ge=1, le=10)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class NextRiskPayload(StrictRequest):
    type: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    date: date
    shortageAmount: int


class CashflowSummaryPayload(StrictRequest):
    lowestBalance: int
    lowestBalanceDate: date


class FactsPayload(StrictRequest):
    safeToSpend: int
    nextRisk: NextRiskPayload
    cashflowSummary: CashflowSummaryPayload


class AIToBackendResponse(StrictRequest):
    schemaVersion: Literal["1.0"] = "1.0"
    analysisId: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
    ]
    riskExplanation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    rankedActions: list[AIActionCandidateRef] = Field(default_factory=list)
    userMessage: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


class BackendToAIRequest(StrictRequest):
    schemaVersion: Literal["1.0"] = "1.0"
    analysisId: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
    ]
    calculatedAt: AwareDatetime
    facts: FactsPayload
    evidence: list[dict] = Field(default_factory=list)
    actionCandidates: list[dict] = Field(default_factory=list)


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
NonNegativeWon = Annotated[int, Field(strict=True, ge=0)]
PositiveWon = Annotated[int, Field(strict=True, gt=0)]
EventType = Literal[
    "RECEIVABLE",
    "CARD_BILL",
    "INSTALLMENT_PAYMENT",
    "RENT",
    "INSURANCE",
    "UTILITY",
    "LOAN_PAYMENT",
    "TAX",
    "SAVINGS",
    "DISCRETIONARY_EXPENSE",
    "OTHER_INFLOW",
    "OTHER_OUTFLOW",
]


class AccountCreate(StrictRequest):
    account_id: Identifier
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    account_type: Literal["CHECKING", "SAVINGS", "OTHER"]
    balance: NonNegativeWon
    minimum_balance: NonNegativeWon = 0
    protected_amount: NonNegativeWon = 0
    is_payment_account: bool = False
    is_available_for_transfer: bool = True
    updated_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def protected_amount_does_not_exceed_balance(self) -> AccountCreate:
        if self.protected_amount > self.balance:
            raise ValueError("protected_amount cannot exceed balance")
        return self


class AccountPatch(StrictRequest):
    name: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ] = None
    account_type: Literal["CHECKING", "SAVINGS", "OTHER"] | None = None
    balance: NonNegativeWon | None = None
    minimum_balance: NonNegativeWon | None = None
    protected_amount: NonNegativeWon | None = None
    is_payment_account: bool | None = None
    is_available_for_transfer: bool | None = None


class TransactionPatch(StrictRequest):
    occurred_at: AwareDatetime | None = None
    direction: Literal["INFLOW", "OUTFLOW"] | None = None
    amount: PositiveWon | None = None
    description: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ] = None
    category: str | None = None
    counterparty_name: str | None = None
    transaction_type: str | None = None
    card_id: Identifier | None = None
    installment_months: Annotated[int, Field(strict=True, ge=2, le=60)] | None = None
    source_reference_id: str | None = None
    is_internal_transfer: bool | None = None


class CounterpartyCreate(StrictRequest):
    counterparty_id: Identifier
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    counterparty_type: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
    ] = "CLIENT"
    is_recurring: bool = False
    payment_history_count: Annotated[int, Field(strict=True, ge=0)] = 0
    updated_at: AwareDatetime | None = None


class CounterpartyPatch(StrictRequest):
    name: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ] = None
    counterparty_type: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)
    ] = None
    is_recurring: bool | None = None


class ScheduledEventCreate(StrictRequest):
    event_id: Identifier
    event_type: EventType
    direction: Literal["INFLOW", "OUTFLOW"]
    amount: PositiveWon
    expected_date: date
    account_id: Identifier
    counterparty_id: Identifier | None = None
    certainty: Literal["CONFIRMED", "ESTIMATED", "UNCERTAIN"] = "ESTIMATED"
    is_essential: bool = False
    is_adjustable: bool = False
    source: Literal["USER_INPUT"] = "USER_INPUT"
    source_reference_id: str | None = None


class ScheduledEventPatch(StrictRequest):
    event_type: EventType | None = None
    direction: Literal["INFLOW", "OUTFLOW"] | None = None
    amount: PositiveWon | None = None
    expected_date: date | None = None
    account_id: Identifier | None = None
    counterparty_id: Identifier | None = None
    certainty: Literal["CONFIRMED", "ESTIMATED", "UNCERTAIN"] | None = None
    is_essential: bool | None = None
    is_adjustable: bool | None = None
    source_reference_id: str | None = None


class ReceivableCreate(StrictRequest):
    receivable_id: Identifier
    counterparty_id: Identifier
    amount: PositiveWon
    expected_date: date
    status: Literal["CONFIRMED", "ESTIMATED", "OVERDUE"] = "ESTIMATED"
    destination_account_id: Identifier
    user_confirmed: bool = True
    source: Literal["USER_INPUT"] = "USER_INPUT"
    updated_at: AwareDatetime | None = None


class ReceivablePatch(StrictRequest):
    counterparty_id: Identifier | None = None
    amount: PositiveWon | None = None
    expected_date: date | None = None
    status: Literal["CONFIRMED", "ESTIMATED", "OVERDUE", "RECEIVED", "CANCELLED"] | None = None
    destination_account_id: Identifier | None = None
    user_confirmed: bool | None = None


class CandidateDecision(StrictRequest):
    decision: Literal["CONFIRMED", "REJECTED", "UNKNOWN"]
    details: dict | None = None


class CandidateCommitDecision(CandidateDecision):
    candidate_id: Identifier


class UserPreferencesRequest(StrictRequest):
    protection_level: Annotated[float, Field(strict=True, ge=0, le=1)] | None = None
    minimum_total_reserve: NonNegativeWon | None = None
    income_type: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ] = None

    @model_validator(mode="after")
    def at_least_one_preference(self) -> UserPreferencesRequest:
        if not self.model_fields_set:
            raise ValueError("at least one preference is required")
        return self


class SetupCommitRequest(StrictRequest):
    preferences: UserPreferencesRequest
    candidates: list[CandidateCommitDecision] = Field(default_factory=list)


class DemoResetRequest(StrictRequest):
    confirmation: Literal["RESET_DEMO"]


class AnalysisRequest(StrictRequest):
    trigger_type: Literal["MANUAL", "DATA_REFRESH", "SCHEDULED", "RECOMMENDATION_APPROVAL"] = (
        "MANUAL"
    )
    as_of: AwareDatetime | None = None


class RecommendationDecision(StrictRequest):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None


class InstallmentPrecheckRequest(StrictRequest):
    purchase_amount: PositiveWon
    installment_months: Annotated[int, Field(strict=True, ge=2, le=60)]
    first_payment_date: date
    card_id: Identifier


class ErrorResponse(BaseModel):
    code: str
    message: str
    details: dict
    retryable: bool


def dumped(model: BaseModel, *, exclude_unset: bool = False) -> dict:
    return model.model_dump(mode="json", exclude_unset=exclude_unset, exclude_none=True)
