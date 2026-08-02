"""Pydantic domain contracts for FlowGuard's deterministic financial analysis."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    """Strict, immutable base model used by financial snapshots and results."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountType(StrEnum):
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    OTHER = "OTHER"


class Direction(StrEnum):
    INFLOW = "INFLOW"
    OUTFLOW = "OUTFLOW"


class TransactionType(StrEnum):
    PURCHASE = "PURCHASE"
    INCOME = "INCOME"
    TRANSFER = "TRANSFER"
    CARD_PAYMENT = "CARD_PAYMENT"
    LOAN_PAYMENT = "LOAN_PAYMENT"
    TAX = "TAX"
    REFUND = "REFUND"
    OTHER = "OTHER"


class ReceivableStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    ESTIMATED = "ESTIMATED"
    OVERDUE = "OVERDUE"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class EventType(StrEnum):
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


class Certainty(StrEnum):
    CONFIRMED = "CONFIRMED"
    ESTIMATED = "ESTIMATED"
    UNCERTAIN = "UNCERTAIN"


class InstallmentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class FundType(StrEnum):
    TAX_RESERVE = "TAX_RESERVE"
    EMERGENCY_RESERVE = "EMERGENCY_RESERVE"
    BUSINESS_RESERVE = "BUSINESS_RESERVE"
    OTHER = "OTHER"


class InputSource(StrEnum):
    CSV = "CSV"
    USER_INPUT = "USER_INPUT"
    USER_CONFIRMED = "USER_CONFIRMED"
    SYSTEM_DERIVED = "SYSTEM_DERIVED"


class DelayScenario(StrEnum):
    ON_TIME = "ON_TIME"
    DELAY_3_DAYS = "DELAY_3_DAYS"
    DELAY_7_DAYS = "DELAY_7_DAYS"
    DELAY_14_DAYS = "DELAY_14_DAYS"


class ShortfallType(StrEnum):
    PAYMENT_ACCOUNT = "PAYMENT_ACCOUNT"
    TOTAL_LIQUIDITY = "TOTAL_LIQUIDITY"


class RiskStatus(StrEnum):
    STABLE = "STABLE"
    VERIFY = "VERIFY"
    PREPARE = "PREPARE"
    ACT_NOW = "ACT_NOW"


class RecentTrend(StrEnum):
    IMPROVING = "IMPROVING"
    STABLE = "STABLE"
    WORSENING = "WORSENING"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class Account(DomainModel):
    account_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    account_type: AccountType
    balance: int = Field(ge=0)
    minimum_balance: int = Field(default=0, ge=0)
    protected_amount: int = Field(default=0, ge=0)
    is_payment_account: bool = False
    is_available_for_transfer: bool = True
    updated_at: datetime

    @model_validator(mode="after")
    def validate_protected_amount(self) -> Account:
        if self.protected_amount > self.balance:
            raise ValueError("protected_amount cannot exceed balance")
        return self

    @property
    def available_balance(self) -> int:
        return max(0, self.balance - self.protected_amount - self.minimum_balance)


class Card(DomainModel):
    card_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    payment_account_id: str = Field(min_length=1)
    payment_day: int = Field(ge=1, le=31)
    current_billing_amount: int = Field(default=0, ge=0)
    billing_date: date
    updated_at: datetime


class Transaction(DomainModel):
    transaction_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    occurred_at: datetime
    direction: Direction
    amount: int = Field(gt=0)
    description: str | None = None
    category: str | None = None
    counterparty_name: str | None = None
    transaction_type: TransactionType = TransactionType.OTHER
    source: InputSource = InputSource.CSV
    card_id: str | None = None
    source_reference_id: str | None = None
    is_internal_transfer: bool = False
    linked_transaction_id: str | None = None


class Counterparty(DomainModel):
    counterparty_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    counterparty_type: str = "CLIENT"
    is_recurring: bool = False
    payment_history_count: int = Field(default=0, ge=0)
    updated_at: datetime


class PaymentHistory(DomainModel):
    payment_history_id: str = Field(min_length=1)
    counterparty_id: str = Field(min_length=1)
    expected_date: date
    actual_date: date
    amount: int = Field(ge=0)

    @property
    def delay_days(self) -> int:
        return max(0, (self.actual_date - self.expected_date).days)


class Receivable(DomainModel):
    receivable_id: str = Field(min_length=1)
    counterparty_id: str = Field(min_length=1)
    amount: int = Field(gt=0)
    expected_date: date
    status: ReceivableStatus
    destination_account_id: str = Field(min_length=1)
    user_confirmed: bool = False
    source: InputSource = InputSource.USER_INPUT
    updated_at: datetime


class ScheduledCashEvent(DomainModel):
    event_id: str = Field(min_length=1)
    event_type: EventType
    direction: Direction
    amount: int = Field(gt=0)
    expected_date: date
    account_id: str = Field(min_length=1)
    counterparty_id: str | None = None
    certainty: Certainty = Certainty.ESTIMATED
    is_essential: bool = False
    is_adjustable: bool = False
    source: InputSource = InputSource.SYSTEM_DERIVED
    source_reference_id: str | None = None


class InstallmentPlan(DomainModel):
    installment_plan_id: str = Field(min_length=1)
    card_id: str = Field(min_length=1)
    original_amount: int = Field(gt=0)
    monthly_payment: int = Field(gt=0)
    total_months: int = Field(gt=0)
    remaining_months: int = Field(ge=0)
    next_payment_date: date
    status: InstallmentStatus = InstallmentStatus.ACTIVE

    @model_validator(mode="after")
    def validate_remaining_months(self) -> InstallmentPlan:
        if self.remaining_months > self.total_months:
            raise ValueError("remaining_months cannot exceed total_months")
        return self


class ProtectedFund(DomainModel):
    protected_fund_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    fund_type: FundType
    amount: int = Field(gt=0)
    release_date: date | None = None
    user_confirmed: bool = False


class UserPreferences(DomainModel):
    protection_level: float = Field(default=0.90, ge=0, le=1)
    minimum_total_reserve: int = Field(default=0, ge=0)


class DataQuality(DomainModel):
    missing_sources: tuple[str, ...] = ()
    stale_sources: tuple[str, ...] = ()
    unconfirmed_items: tuple[str, ...] = ()


class FinancialSnapshot(DomainModel):
    snapshot_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    as_of: datetime
    timezone: str = "Asia/Seoul"
    accounts: tuple[Account, ...]
    cards: tuple[Card, ...] = ()
    transactions: tuple[Transaction, ...] = ()
    counterparties: tuple[Counterparty, ...] = ()
    payment_histories: tuple[PaymentHistory, ...] = ()
    scheduled_events: tuple[ScheduledCashEvent, ...] = ()
    receivables: tuple[Receivable, ...] = ()
    installment_plans: tuple[InstallmentPlan, ...] = ()
    protected_funds: tuple[ProtectedFund, ...] = ()
    preferences: UserPreferences = UserPreferences()
    data_quality: DataQuality = DataQuality()

    @model_validator(mode="after")
    def validate_snapshot_references(self) -> FinancialSnapshot:
        account_ids = [account.account_id for account in self.accounts]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("account_id values must be unique")
        if not account_ids:
            raise ValueError("at least one account is required")

        known_accounts = set(account_ids)
        referenced_accounts = {
            *(card.payment_account_id for card in self.cards),
            *(transaction.account_id for transaction in self.transactions),
            *(receivable.destination_account_id for receivable in self.receivables),
            *(event.account_id for event in self.scheduled_events),
            *(fund.account_id for fund in self.protected_funds),
        }
        unknown_accounts = referenced_accounts - known_accounts
        if unknown_accounts:
            raise ValueError(f"unknown account references: {sorted(unknown_accounts)}")

        entity_id_groups = (
            [card.card_id for card in self.cards],
            [transaction.transaction_id for transaction in self.transactions],
            [counterparty.counterparty_id for counterparty in self.counterparties],
            [history.payment_history_id for history in self.payment_histories],
            [receivable.receivable_id for receivable in self.receivables],
            [event.event_id for event in self.scheduled_events],
            [plan.installment_plan_id for plan in self.installment_plans],
            [fund.protected_fund_id for fund in self.protected_funds],
        )
        if any(len(ids) != len(set(ids)) for ids in entity_id_groups):
            raise ValueError("entity identifiers must be unique within each collection")

        card_ids = {card.card_id for card in self.cards}
        unknown_cards = {
            *(plan.card_id for plan in self.installment_plans),
            *(transaction.card_id for transaction in self.transactions if transaction.card_id),
        } - card_ids
        if unknown_cards:
            raise ValueError(f"unknown card references: {sorted(unknown_cards)}")

        counterparty_ids = {counterparty.counterparty_id for counterparty in self.counterparties}
        referenced_counterparties = {
            *(history.counterparty_id for history in self.payment_histories),
            *(receivable.counterparty_id for receivable in self.receivables),
            *(
                event.counterparty_id
                for event in self.scheduled_events
                if event.counterparty_id is not None
            ),
        }
        if counterparty_ids:
            unknown_counterparties = referenced_counterparties - counterparty_ids
            if unknown_counterparties:
                raise ValueError(
                    f"unknown counterparty references: {sorted(unknown_counterparties)}"
                )
        return self


class DailyPosition(DomainModel):
    date: date
    account_balances: dict[str, int]
    total_balance: int
    available_balance: int
    protected_balance: int
    status: RiskStatus
    triggering_event_ids: tuple[str, ...] = ()


class ShortfallIncident(DomainModel):
    date: date
    shortfall_type: ShortfallType
    gap: int = Field(ge=0)
    account_id: str
    event_id: str | None = None
    is_essential: bool = False


class ScenarioCashflow(DomainModel):
    scenario: DelayScenario
    weight: float = Field(ge=0, le=1)
    daily_positions: tuple[DailyPosition, ...]
    shortfalls: tuple[ShortfallIncident, ...] = ()

    @property
    def first_shortfall(self) -> ShortfallIncident | None:
        return self.shortfalls[0] if self.shortfalls else None


class RiskMetrics(DomainModel):
    shortfall_probability: float = Field(ge=0, le=1)
    payment_account_shortfall_probability: float = Field(ge=0, le=1)
    total_liquidity_shortfall_probability: float = Field(ge=0, le=1)
    first_risk_date: date | None = None
    shortfall_type: ShortfallType | None = None
    expected_gap_min: int = Field(ge=0)
    expected_gap_max: int = Field(ge=0)
    days_until_risk: int | None = None
    data_confidence: float = Field(ge=0, le=1)
    has_essential_risk: bool = False
    requires_verification: bool = False
    triggering_event_ids: tuple[str, ...] = ()


class CashflowAnalysis(DomainModel):
    snapshot_id: str
    analysis_horizon_days: int = Field(gt=0)
    daily_positions: tuple[DailyPosition, ...]
    scenarios: tuple[ScenarioCashflow, ...]
    risk_metrics: RiskMetrics
    seed: int
    tool_version: str
    model_version: str


class BindingConstraint(DomainModel):
    date: date
    event_id: str | None = None
    reason: str


class SafeToSpendResult(DomainModel):
    snapshot_id: str
    safe_to_spend: int = Field(ge=0)
    protection_level: float = Field(ge=0, le=1)
    binding_constraint: BindingConstraint | None = None
    data_confidence: float = Field(ge=0, le=1)
    seed: int
    tool_version: str


class CounterpartyEvidence(DomainModel):
    counterparty_id: str
    payment_history_count: int = Field(ge=0)
    on_time_rate: float = Field(ge=0, le=1)
    average_delay_days: float = Field(ge=0)
    median_delay_days: float = Field(ge=0)
    maximum_delay_days: int = Field(ge=0)
    recent_trend: RecentTrend
    data_confidence: float = Field(ge=0, le=1)


class RiskPresentation(DomainModel):
    status: RiskStatus
    status_label: str
    title: str
    impact: str
    cause: str
    recommended_action: str
    confidence_label: str
    rule_version: str


class ActionType(StrEnum):
    TRANSFER = "transfer"
    RESERVE_FUNDS = "reserve_funds"
    ADJUST_DISCRETIONARY_BUDGET = "adjust_discretionary_budget"
    PAUSE_SAVINGS = "pause_savings"
    SHIFT_PAYMENT_DATE = "shift_payment_date"
    DELAY_PURCHASE = "delay_purchase"
    ADD_INSTALLMENT = "add_installment"
    CONFIRM_RECEIVABLE = "confirm_receivable"


class ActionMetadata(DomainModel):
    action_id: str = ""
    requires_user_approval: Literal[True] = True
    assumptions: tuple[str, ...] = ()
    source_evidence_ids: tuple[str, ...] = ()


class TransferParameters(DomainModel):
    from_account_id: str
    to_account_id: str
    amount: int = Field(gt=0)
    execution_date: date


class ReserveFundsParameters(DomainModel):
    account_id: str
    amount: int = Field(gt=0)
    fund_type: FundType


class AdjustDiscretionaryBudgetParameters(DomainModel):
    amount: int = Field(gt=0)
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self) -> AdjustDiscretionaryBudgetParameters:
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot precede start_date")
        return self


class PauseSavingsParameters(DomainModel):
    event_ids: tuple[str, ...]
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_dates(self) -> PauseSavingsParameters:
        if self.end_date < self.start_date:
            raise ValueError("end_date cannot precede start_date")
        return self


class ShiftPaymentDateParameters(DomainModel):
    event_id: str
    from_date: date
    to_date: date

    @model_validator(mode="after")
    def validate_dates(self) -> ShiftPaymentDateParameters:
        if self.to_date <= self.from_date:
            raise ValueError("to_date must be later than from_date")
        return self


class DelayPurchaseParameters(DomainModel):
    amount: int = Field(gt=0)
    from_date: date
    to_date: date

    @model_validator(mode="after")
    def validate_dates(self) -> DelayPurchaseParameters:
        if self.to_date <= self.from_date:
            raise ValueError("to_date must be later than from_date")
        return self


class AddInstallmentParameters(DomainModel):
    purchase_amount: int = Field(gt=0)
    installment_months: int = Field(gt=0)
    first_payment_date: date
    card_id: str


class ConfirmReceivableParameters(DomainModel):
    receivable_id: str


class TransferAction(ActionMetadata):
    type: Literal["transfer"] = "transfer"
    parameters: TransferParameters


class ReserveFundsAction(ActionMetadata):
    type: Literal["reserve_funds"] = "reserve_funds"
    parameters: ReserveFundsParameters


class AdjustDiscretionaryBudgetAction(ActionMetadata):
    type: Literal["adjust_discretionary_budget"] = "adjust_discretionary_budget"
    parameters: AdjustDiscretionaryBudgetParameters


class PauseSavingsAction(ActionMetadata):
    type: Literal["pause_savings"] = "pause_savings"
    parameters: PauseSavingsParameters


class ShiftPaymentDateAction(ActionMetadata):
    type: Literal["shift_payment_date"] = "shift_payment_date"
    parameters: ShiftPaymentDateParameters


class DelayPurchaseAction(ActionMetadata):
    type: Literal["delay_purchase"] = "delay_purchase"
    parameters: DelayPurchaseParameters


class AddInstallmentAction(ActionMetadata):
    type: Literal["add_installment"] = "add_installment"
    parameters: AddInstallmentParameters


class ConfirmReceivableAction(ActionMetadata):
    type: Literal["confirm_receivable"] = "confirm_receivable"
    parameters: ConfirmReceivableParameters


Action = Annotated[
    TransferAction
    | ReserveFundsAction
    | AdjustDiscretionaryBudgetAction
    | PauseSavingsAction
    | ShiftPaymentDateAction
    | DelayPurchaseAction
    | AddInstallmentAction
    | ConfirmReceivableAction,
    Field(discriminator="type"),
]


class PolicyViolation(DomainModel):
    code: str
    message: str
    action_id: str | None = None


class PolicyResult(DomainModel):
    valid: bool
    violations: tuple[PolicyViolation, ...] = ()
    requires_user_approval: bool
    policy_version: str


class RiskShift(DomainModel):
    detected: bool
    source_date: date | None = None
    target_date: date | None = None
    reasons: tuple[str, ...] = ()


class ActionEvaluation(DomainModel):
    snapshot_id: str
    valid: bool
    before: CashflowAnalysis
    after: CashflowAnalysis
    risk_shift: RiskShift
    policy_violations: tuple[PolicyViolation, ...] = ()
    requires_user_approval: bool
    tool_version: str
