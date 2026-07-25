"""Closed action catalog, virtual application, rebound checks, and policy validation."""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from pydantic import TypeAdapter

from flowguard.config import (
    ACTION_EVALUATOR_TOOL_VERSION,
    ANALYSIS_HORIZON_DAYS,
    DEFAULT_SIMULATION_SEED,
    MIN_ACTION_GAP_IMPROVEMENT_WON,
    POLICY_VERSION,
    REBOUND_SAFE_TO_SPEND_DROP_WON,
    STALE_SOURCE_DAYS,
)
from flowguard.domain import (
    Action,
    ActionEvaluation,
    AddInstallmentAction,
    AdjustDiscretionaryBudgetAction,
    CashflowAnalysis,
    Certainty,
    ConfirmReceivableAction,
    DelayPurchaseAction,
    Direction,
    EventType,
    FinancialSnapshot,
    InputSource,
    InstallmentPlan,
    PauseSavingsAction,
    PolicyResult,
    PolicyViolation,
    ProtectedFund,
    ReceivableStatus,
    ReserveFundsAction,
    RiskShift,
    ScheduledCashEvent,
    ShiftPaymentDateAction,
    TransferAction,
)

from .cashflow import calculate_safe_to_spend, simulate_cashflow

_ACTION_LIST_ADAPTER = TypeAdapter(list[Action])


def _parse_actions(
    actions: Sequence[Action | Mapping[str, Any]],
) -> tuple[Action, ...]:
    parsed = _ACTION_LIST_ADAPTER.validate_python(list(actions))
    return tuple(
        action
        if action.action_id
        else action.model_copy(update={"action_id": f"action-{index + 1}"})
        for index, action in enumerate(parsed)
    )


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _protected_by_account(snapshot: FinancialSnapshot) -> dict[str, int]:
    fund_totals = {account.account_id: 0 for account in snapshot.accounts}
    for fund in snapshot.protected_funds:
        fund_totals[fund.account_id] += fund.amount
    return {
        account.account_id: max(account.protected_amount, fund_totals[account.account_id])
        for account in snapshot.accounts
    }


def _available_balance(snapshot: FinancialSnapshot, account_id: str) -> int:
    account = next(account for account in snapshot.accounts if account.account_id == account_id)
    protected = _protected_by_account(snapshot)[account_id]
    return max(0, account.balance - protected - account.minimum_balance)


def _apply_transfer(
    snapshot: FinancialSnapshot,
    action: TransferAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    account_ids = {account.account_id for account in snapshot.accounts}
    if parameters.from_account_id == parameters.to_account_id:
        raise ValueError("transfer accounts must be different")
    if {parameters.from_account_id, parameters.to_account_id} - account_ids:
        raise ValueError("transfer references an unknown account")
    if parameters.amount > _available_balance(snapshot, parameters.from_account_id):
        raise ValueError("transfer exceeds non-protected available balance")
    reference = f"virtual-transfer:{action.action_id}"
    outflow = ScheduledCashEvent(
        event_id=f"{reference}:0-out",
        event_type=EventType.OTHER_OUTFLOW,
        direction=Direction.OUTFLOW,
        amount=parameters.amount,
        expected_date=parameters.execution_date,
        account_id=parameters.from_account_id,
        certainty=Certainty.CONFIRMED,
        is_essential=False,
        is_adjustable=False,
        source=InputSource.SYSTEM_DERIVED,
        source_reference_id=f"{reference}:out",
    )
    inflow = ScheduledCashEvent(
        event_id=f"{reference}:1-in",
        event_type=EventType.OTHER_INFLOW,
        direction=Direction.INFLOW,
        amount=parameters.amount,
        expected_date=parameters.execution_date,
        account_id=parameters.to_account_id,
        certainty=Certainty.CONFIRMED,
        is_essential=False,
        is_adjustable=False,
        source=InputSource.SYSTEM_DERIVED,
        source_reference_id=f"{reference}:in",
    )
    return snapshot.model_copy(
        update={"scheduled_events": (*snapshot.scheduled_events, outflow, inflow)}
    )


def _apply_reserve_funds(
    snapshot: FinancialSnapshot,
    action: ReserveFundsAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    if parameters.account_id not in {account.account_id for account in snapshot.accounts}:
        raise ValueError("reserve_funds references an unknown account")
    if parameters.amount > _available_balance(snapshot, parameters.account_id):
        raise ValueError("reserve_funds exceeds available balance")
    accounts = tuple(
        account.model_copy(
            update={"protected_amount": account.protected_amount + parameters.amount}
        )
        if account.account_id == parameters.account_id
        else account
        for account in snapshot.accounts
    )
    fund = ProtectedFund(
        protected_fund_id=f"virtual-protected:{action.action_id}",
        account_id=parameters.account_id,
        fund_type=parameters.fund_type,
        amount=parameters.amount,
        user_confirmed=True,
    )
    return snapshot.model_copy(
        update={
            "accounts": accounts,
            "protected_funds": (*snapshot.protected_funds, fund),
        }
    )


def _apply_adjust_discretionary_budget(
    snapshot: FinancialSnapshot,
    action: AdjustDiscretionaryBudgetAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    remaining = parameters.amount
    adjusted: list[ScheduledCashEvent] = []
    for event in sorted(
        snapshot.scheduled_events,
        key=lambda candidate: (candidate.expected_date, candidate.event_id),
    ):
        eligible = (
            event.direction == Direction.OUTFLOW
            and not event.is_essential
            and (event.is_adjustable or event.event_type == EventType.DISCRETIONARY_EXPENSE)
            and parameters.start_date <= event.expected_date <= parameters.end_date
        )
        if not eligible or remaining == 0:
            adjusted.append(event)
            continue
        reduction = min(event.amount, remaining)
        remaining -= reduction
        if reduction < event.amount:
            adjusted.append(event.model_copy(update={"amount": event.amount - reduction}))
    if remaining:
        raise ValueError("discretionary budget reduction exceeds adjustable expenses")
    return snapshot.model_copy(update={"scheduled_events": tuple(adjusted)})


def _apply_pause_savings(
    snapshot: FinancialSnapshot,
    action: PauseSavingsAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    target_ids = set(parameters.event_ids)
    matched: set[str] = set()
    retained: list[ScheduledCashEvent] = []
    for event in snapshot.scheduled_events:
        if event.event_id not in target_ids:
            retained.append(event)
            continue
        if (
            event.event_type != EventType.SAVINGS
            or not event.is_adjustable
            or not parameters.start_date <= event.expected_date <= parameters.end_date
        ):
            raise ValueError("pause_savings can only target adjustable savings events")
        matched.add(event.event_id)
    if matched != target_ids:
        raise ValueError("pause_savings references an unknown event")
    return snapshot.model_copy(update={"scheduled_events": tuple(retained)})


def _apply_shift_payment_date(
    snapshot: FinancialSnapshot,
    action: ShiftPaymentDateAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    found = False
    events: list[ScheduledCashEvent] = []
    for event in snapshot.scheduled_events:
        if event.event_id != parameters.event_id:
            events.append(event)
            continue
        if event.expected_date != parameters.from_date or not event.is_adjustable:
            raise ValueError("payment event is not eligible for date shifting")
        events.append(event.model_copy(update={"expected_date": parameters.to_date}))
        found = True
    if not found:
        raise ValueError("shift_payment_date references an unknown event")
    return snapshot.model_copy(update={"scheduled_events": tuple(events)})


def _apply_delay_purchase(
    snapshot: FinancialSnapshot,
    action: DelayPurchaseAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    found = False
    events: list[ScheduledCashEvent] = []
    for event in snapshot.scheduled_events:
        matches = (
            not found
            and event.event_type == EventType.DISCRETIONARY_EXPENSE
            and event.is_adjustable
            and event.certainty != Certainty.CONFIRMED
            and event.amount == parameters.amount
            and event.expected_date == parameters.from_date
        )
        if matches:
            events.append(event.model_copy(update={"expected_date": parameters.to_date}))
            found = True
        else:
            events.append(event)
    if not found:
        raise ValueError("delay_purchase requires a matching unconfirmed purchase")
    return snapshot.model_copy(update={"scheduled_events": tuple(events)})


def _apply_add_installment(
    snapshot: FinancialSnapshot,
    action: AddInstallmentAction,
) -> FinancialSnapshot:
    parameters = action.parameters
    card = next(
        (card for card in snapshot.cards if card.card_id == parameters.card_id),
        None,
    )
    if card is None:
        raise ValueError("add_installment references an unknown card")
    quotient, remainder = divmod(
        parameters.purchase_amount,
        parameters.installment_months,
    )
    if quotient == 0:
        raise ValueError("installment_months cannot exceed purchase amount in Won")
    plan_id = f"virtual-installment:{action.action_id}"
    plan = InstallmentPlan(
        installment_plan_id=plan_id,
        card_id=parameters.card_id,
        original_amount=parameters.purchase_amount,
        monthly_payment=quotient + int(remainder > 0),
        total_months=parameters.installment_months,
        remaining_months=parameters.installment_months,
        next_payment_date=parameters.first_payment_date,
    )
    payment_events = tuple(
        ScheduledCashEvent(
            event_id=f"{plan_id}:{index + 1}",
            event_type=EventType.INSTALLMENT_PAYMENT,
            direction=Direction.OUTFLOW,
            amount=quotient + int(index < remainder),
            expected_date=_add_months(parameters.first_payment_date, index),
            account_id=card.payment_account_id,
            certainty=Certainty.CONFIRMED,
            is_essential=True,
            is_adjustable=False,
            source=InputSource.SYSTEM_DERIVED,
            source_reference_id=plan_id,
        )
        for index in range(parameters.installment_months)
    )
    return snapshot.model_copy(
        update={
            "installment_plans": (*snapshot.installment_plans, plan),
            "scheduled_events": (*snapshot.scheduled_events, *payment_events),
        }
    )


def _apply_confirm_receivable(
    snapshot: FinancialSnapshot,
    action: ConfirmReceivableAction,
) -> FinancialSnapshot:
    receivable = next(
        (
            receivable
            for receivable in snapshot.receivables
            if receivable.receivable_id == action.parameters.receivable_id
        ),
        None,
    )
    if receivable is None:
        raise ValueError("confirm_receivable references an unknown receivable")
    if receivable.status in {ReceivableStatus.RECEIVED, ReceivableStatus.CANCELLED}:
        raise ValueError("confirm_receivable cannot target a closed receivable")
    # Confirmation is evidence gathering. The financial state changes only after a new,
    # externally confirmed snapshot is built.
    return snapshot


def apply_actions_virtual(
    snapshot: FinancialSnapshot,
    actions: Sequence[Action | Mapping[str, Any]],
) -> FinancialSnapshot:
    """Apply catalogued actions to a copied snapshot without external side effects."""

    result = snapshot
    for action in _parse_actions(actions):
        if isinstance(action, TransferAction):
            result = _apply_transfer(result, action)
        elif isinstance(action, ReserveFundsAction):
            result = _apply_reserve_funds(result, action)
        elif isinstance(action, AdjustDiscretionaryBudgetAction):
            result = _apply_adjust_discretionary_budget(result, action)
        elif isinstance(action, PauseSavingsAction):
            result = _apply_pause_savings(result, action)
        elif isinstance(action, ShiftPaymentDateAction):
            result = _apply_shift_payment_date(result, action)
        elif isinstance(action, DelayPurchaseAction):
            result = _apply_delay_purchase(result, action)
        elif isinstance(action, AddInstallmentAction):
            result = _apply_add_installment(result, action)
        elif isinstance(action, ConfirmReceivableAction):
            result = _apply_confirm_receivable(result, action)
    return result


def _policy_violation(
    code: str,
    message: str,
    action_id: str | None = None,
) -> PolicyViolation:
    return PolicyViolation(code=code, message=message, action_id=action_id)


def _action_feasibility_violations(
    snapshot: FinancialSnapshot,
    action: Action,
) -> list[PolicyViolation]:
    violations: list[PolicyViolation] = []
    action_id = action.action_id
    account_ids = {account.account_id for account in snapshot.accounts}
    event_by_id = {event.event_id: event for event in snapshot.scheduled_events}
    analysis_end = snapshot.as_of.date() + timedelta(days=ANALYSIS_HORIZON_DAYS - 1)

    if isinstance(action, TransferAction):
        parameters = action.parameters
        if parameters.from_account_id == parameters.to_account_id:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "출금계좌와 입금계좌는 달라야 합니다.",
                    action_id,
                )
            )
        elif {parameters.from_account_id, parameters.to_account_id} - account_ids:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "이체 대상 계좌를 찾을 수 없습니다.",
                    action_id,
                )
            )
        elif parameters.amount > _available_balance(snapshot, parameters.from_account_id):
            violations.append(
                _policy_violation(
                    "PROTECTED_FUND_VIOLATION",
                    "보호자금 또는 최소 안전잔액을 이체에 사용할 수 없습니다.",
                    action_id,
                )
            )
        if not snapshot.as_of.date() <= parameters.execution_date <= analysis_end:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "이체일은 13주 분석기간 안이어야 합니다.",
                    action_id,
                )
            )
    elif isinstance(action, ReserveFundsAction):
        if action.parameters.account_id not in account_ids:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "보호자금을 설정할 계좌를 찾을 수 없습니다.",
                    action_id,
                )
            )
        elif action.parameters.amount > _available_balance(
            snapshot,
            action.parameters.account_id,
        ):
            violations.append(
                _policy_violation(
                    "MINIMUM_BALANCE_VIOLATION",
                    "가용잔액을 초과해 보호자금으로 지정할 수 없습니다.",
                    action_id,
                )
            )
    elif isinstance(action, AdjustDiscretionaryBudgetAction):
        adjustable = sum(
            event.amount
            for event in snapshot.scheduled_events
            if event.direction == Direction.OUTFLOW
            and not event.is_essential
            and (event.is_adjustable or event.event_type == EventType.DISCRETIONARY_EXPENSE)
            and action.parameters.start_date <= event.expected_date <= action.parameters.end_date
        )
        if action.parameters.amount > adjustable:
            violations.append(
                _policy_violation(
                    "ESSENTIAL_EXPENSE_PROTECTION",
                    "선택지출 감소 가능 금액을 초과했습니다.",
                    action_id,
                )
            )
    elif isinstance(action, PauseSavingsAction):
        for event_id in action.parameters.event_ids:
            event = event_by_id.get(event_id)
            if event is None or event.event_type != EventType.SAVINGS or not event.is_adjustable:
                violations.append(
                    _policy_violation(
                        "ESSENTIAL_EXPENSE_PROTECTION",
                        "조정 가능한 저축 이벤트만 중지할 수 있습니다.",
                        action_id,
                    )
                )
                break
    elif isinstance(action, ShiftPaymentDateAction):
        event = event_by_id.get(action.parameters.event_id)
        if (
            event is None
            or not event.is_adjustable
            or event.expected_date != action.parameters.from_date
        ):
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "결제일을 변경할 수 없는 이벤트입니다.",
                    action_id,
                )
            )
        elif action.parameters.to_date > analysis_end:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "변경된 결제일은 13주 분석기간 안이어야 합니다.",
                    action_id,
                )
            )
    elif isinstance(action, DelayPurchaseAction):
        matching_purchase = any(
            event.event_type == EventType.DISCRETIONARY_EXPENSE
            and event.is_adjustable
            and event.certainty != Certainty.CONFIRMED
            and event.amount == action.parameters.amount
            and event.expected_date == action.parameters.from_date
            for event in snapshot.scheduled_events
        )
        if not matching_purchase:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "확정되지 않은 구매 계획만 연기할 수 있습니다.",
                    action_id,
                )
            )
    elif isinstance(action, AddInstallmentAction):
        if action.parameters.card_id not in {card.card_id for card in snapshot.cards}:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "할부에 사용할 카드를 찾을 수 없습니다.",
                    action_id,
                )
            )
        elif not snapshot.as_of.date() <= action.parameters.first_payment_date <= analysis_end:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "첫 할부 결제일은 13주 분석기간 안이어야 합니다.",
                    action_id,
                )
            )
    elif isinstance(action, ConfirmReceivableAction):
        receivable = next(
            (
                receivable
                for receivable in snapshot.receivables
                if receivable.receivable_id == action.parameters.receivable_id
            ),
            None,
        )
        if receivable is None or receivable.status in {
            ReceivableStatus.RECEIVED,
            ReceivableStatus.CANCELLED,
        }:
            violations.append(
                _policy_violation(
                    "ACTION_NOT_FEASIBLE",
                    "확인할 수 있는 예정 수입이 아닙니다.",
                    action_id,
                )
            )
    return violations


def validate_financial_policy(
    snapshot: FinancialSnapshot,
    actions: Sequence[Action | Mapping[str, Any]],
    *,
    evaluation: CashflowAnalysis | None = None,
) -> PolicyResult:
    """Validate mandatory financial safety policies for catalogued actions."""

    parsed_actions = _parse_actions(actions)
    violations: list[PolicyViolation] = []
    for action in parsed_actions:
        violations.extend(_action_feasibility_violations(snapshot, action))

    stale_cutoff = snapshot.as_of - timedelta(days=STALE_SOURCE_DAYS)
    updated_values = (
        *(account.updated_at for account in snapshot.accounts),
        *(card.updated_at for card in snapshot.cards),
        *(receivable.updated_at for receivable in snapshot.receivables),
    )
    if snapshot.data_quality.stale_sources or any(
        updated_at < stale_cutoff for updated_at in updated_values
    ):
        violations.append(
            _policy_violation(
                "SNAPSHOT_STALE",
                "오래된 금융정보를 최신 데이터로 다시 확인해야 합니다.",
            )
        )

    unique = {
        (violation.code, violation.message, violation.action_id): violation
        for violation in violations
    }
    return PolicyResult(
        valid=not unique,
        violations=tuple(unique.values()),
        requires_user_approval=bool(parsed_actions),
        policy_version=POLICY_VERSION,
    )


def _detect_rebound_risk(
    snapshot: FinancialSnapshot,
    virtual_snapshot: FinancialSnapshot,
    before: CashflowAnalysis,
    after: CashflowAnalysis,
    *,
    seed: int,
) -> RiskShift:
    reasons: list[str] = []
    before_essential_dates = {
        incident.date
        for scenario in before.scenarios
        for incident in scenario.shortfalls
        if incident.is_essential
    }
    after_essential_dates = {
        incident.date
        for scenario in after.scenarios
        for incident in scenario.shortfalls
        if incident.is_essential
    }
    new_essential_dates = sorted(after_essential_dates - before_essential_dates)
    if new_essential_dates:
        reasons.append("NEW_ESSENTIAL_SHORTFALL")

    before_metrics = before.risk_metrics
    after_metrics = after.risk_metrics
    if (
        before_metrics.first_risk_date is not None
        and after_metrics.first_risk_date is not None
        and after_metrics.first_risk_date > before_metrics.first_risk_date
        and before_metrics.first_risk_date not in after_essential_dates
    ):
        reasons.append("RISK_MOVED_LATER")
    if (
        after_metrics.total_liquidity_shortfall_probability
        > before_metrics.total_liquidity_shortfall_probability
    ):
        reasons.append("TOTAL_LIQUIDITY_RISK_INCREASED")
    if after_metrics.expected_gap_max > before_metrics.expected_gap_max:
        reasons.append("EXPECTED_GAP_INCREASED")

    before_safe = calculate_safe_to_spend(snapshot, seed=seed).safe_to_spend
    after_safe = calculate_safe_to_spend(virtual_snapshot, seed=seed).safe_to_spend
    if before_safe - after_safe >= REBOUND_SAFE_TO_SPEND_DROP_WON:
        reasons.append("SAFE_TO_SPEND_DECREASED")

    target_candidates = new_essential_dates
    if after_metrics.first_risk_date is not None:
        target_candidates = [*target_candidates, after_metrics.first_risk_date]
    return RiskShift(
        detected=bool(reasons),
        source_date=before_metrics.first_risk_date,
        target_date=min(target_candidates) if target_candidates else None,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _is_effective_action_plan(
    actions: tuple[Action, ...],
    before: CashflowAnalysis,
    after: CashflowAnalysis,
) -> bool:
    if not actions:
        return False
    if all(isinstance(action, ConfirmReceivableAction) for action in actions):
        return True

    before_metrics = before.risk_metrics
    after_metrics = after.risk_metrics
    if before_metrics.shortfall_type is None:
        return after_metrics.shortfall_probability == 0

    if before_metrics.shortfall_type.value == "PAYMENT_ACCOUNT":
        before_target_probability = before_metrics.payment_account_shortfall_probability
        after_target_probability = after_metrics.payment_account_shortfall_probability
    else:
        before_target_probability = before_metrics.total_liquidity_shortfall_probability
        after_target_probability = after_metrics.total_liquidity_shortfall_probability
    if after_target_probability < before_target_probability:
        return True
    return (
        before_metrics.expected_gap_max - after_metrics.expected_gap_max
        >= MIN_ACTION_GAP_IMPROVEMENT_WON
    )


def evaluate_action_plan(
    snapshot: FinancialSnapshot,
    actions: Sequence[Action | Mapping[str, Any]],
    *,
    seed: int = DEFAULT_SIMULATION_SEED,
) -> ActionEvaluation:
    """Virtually apply actions and compare deterministic before/after results."""

    parsed_actions = _parse_actions(actions)
    before = simulate_cashflow(snapshot, seed=seed)
    initial_policy = validate_financial_policy(snapshot, parsed_actions)
    if not initial_policy.valid:
        return ActionEvaluation(
            snapshot_id=snapshot.snapshot_id,
            valid=False,
            before=before,
            after=before,
            risk_shift=RiskShift(detected=False),
            policy_violations=initial_policy.violations,
            requires_user_approval=initial_policy.requires_user_approval,
            tool_version=ACTION_EVALUATOR_TOOL_VERSION,
        )

    try:
        virtual_snapshot = apply_actions_virtual(snapshot, parsed_actions)
    except ValueError as exc:
        violation = _policy_violation("ACTION_NOT_FEASIBLE", str(exc))
        return ActionEvaluation(
            snapshot_id=snapshot.snapshot_id,
            valid=False,
            before=before,
            after=before,
            risk_shift=RiskShift(detected=False),
            policy_violations=(violation,),
            requires_user_approval=bool(parsed_actions),
            tool_version=ACTION_EVALUATOR_TOOL_VERSION,
        )

    after = simulate_cashflow(virtual_snapshot, seed=seed)
    risk_shift = _detect_rebound_risk(
        snapshot,
        virtual_snapshot,
        before,
        after,
        seed=seed,
    )
    policy = validate_financial_policy(snapshot, parsed_actions, evaluation=after)
    effective = _is_effective_action_plan(parsed_actions, before, after)
    violations = list(policy.violations)
    if not effective:
        violations.append(
            _policy_violation(
                "ACTION_INEFFECTIVE",
                "대응안이 기준 위험을 의미 있게 개선하지 못합니다.",
            )
        )
    return ActionEvaluation(
        snapshot_id=snapshot.snapshot_id,
        valid=policy.valid and effective and not risk_shift.detected,
        before=before,
        after=after,
        risk_shift=risk_shift,
        policy_violations=tuple(violations),
        requires_user_approval=policy.requires_user_approval,
        tool_version=ACTION_EVALUATOR_TOOL_VERSION,
    )
