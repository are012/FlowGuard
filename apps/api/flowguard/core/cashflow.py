"""Deterministic 91-day cash-flow simulation and Safe-to-Spend calculation."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from flowguard.config import (
    ANALYSIS_HORIZON_DAYS,
    CASHFLOW_TOOL_VERSION,
    DATA_CONFIDENCE_MISSING_SOURCE_PENALTY,
    DATA_CONFIDENCE_STALE_SOURCE_PENALTY,
    DATA_CONFIDENCE_UNCONFIRMED_ITEM_PENALTY,
    DEFAULT_SIMULATION_SEED,
    DELAY_MODEL_VERSION,
    DELAY_SCENARIO_DAYS,
    DELAY_SCENARIO_WEIGHTS,
    MINIMUM_DATA_CONFIDENCE,
    SAFE_TO_SPEND_TOOL_VERSION,
    STALE_SOURCE_DAYS,
)
from flowguard.domain import (
    Account,
    BindingConstraint,
    CashflowAnalysis,
    Certainty,
    DailyPosition,
    DelayScenario,
    Direction,
    EventType,
    FinancialSnapshot,
    InputSource,
    InstallmentStatus,
    ReceivableStatus,
    RiskMetrics,
    RiskStatus,
    SafeToSpendResult,
    ScenarioCashflow,
    ScheduledCashEvent,
    ShortfallIncident,
    ShortfallType,
)

from .presentation import select_risk_status


@dataclass(frozen=True, slots=True)
class _CoreEvent:
    event_id: str
    event_type: EventType
    direction: Direction
    amount: int
    expected_date: date
    account_id: str
    certainty: Certainty
    is_essential: bool
    is_adjustable: bool
    source_reference_id: str | None
    source: str


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _protected_by_account(snapshot: FinancialSnapshot) -> dict[str, int]:
    """Return protected floors without double-counting account and fund records."""

    fund_totals = {account.account_id: 0 for account in snapshot.accounts}
    for fund in snapshot.protected_funds:
        fund_totals[fund.account_id] += fund.amount
    return {
        account.account_id: max(account.protected_amount, fund_totals[account.account_id])
        for account in snapshot.accounts
    }


def _event_from_scheduled(event: ScheduledCashEvent) -> _CoreEvent:
    return _CoreEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        direction=event.direction,
        amount=event.amount,
        expected_date=event.expected_date,
        account_id=event.account_id,
        certainty=event.certainty,
        is_essential=event.is_essential,
        is_adjustable=event.is_adjustable,
        source_reference_id=event.source_reference_id,
        source=event.source.value,
    )


def _scheduled_match(
    events: list[_CoreEvent],
    *,
    source_reference_id: str,
    event_type: EventType,
    expected_date: date,
    account_id: str,
    amount: int,
) -> bool:
    return any(
        (
            event.source_reference_id == source_reference_id
            and event.event_type == event_type
            and event.expected_date == expected_date
        )
        or (
            event.event_type == event_type
            and event.expected_date == expected_date
            and event.account_id == account_id
            and event.amount == amount
        )
        for event in events
    )


def _normalized_events(snapshot: FinancialSnapshot) -> tuple[_CoreEvent, ...]:
    events: list[_CoreEvent] = []
    seen_source_dates: set[tuple[str, date, Direction]] = set()
    for scheduled in snapshot.scheduled_events:
        source_key = (
            scheduled.source_reference_id,
            scheduled.expected_date,
            scheduled.direction,
        )
        if scheduled.source_reference_id and source_key in seen_source_dates:
            continue
        if scheduled.source_reference_id:
            seen_source_dates.add(source_key)
        events.append(_event_from_scheduled(scheduled))

    for receivable in snapshot.receivables:
        if receivable.status in {ReceivableStatus.RECEIVED, ReceivableStatus.CANCELLED}:
            continue
        expected_date = max(snapshot.as_of.date(), receivable.expected_date)
        if _scheduled_match(
            events,
            source_reference_id=receivable.receivable_id,
            event_type=EventType.RECEIVABLE,
            expected_date=expected_date,
            account_id=receivable.destination_account_id,
            amount=receivable.amount,
        ):
            continue
        certainty = (
            Certainty.CONFIRMED
            if receivable.status == ReceivableStatus.CONFIRMED and receivable.user_confirmed
            else Certainty.ESTIMATED
        )
        events.append(
            _CoreEvent(
                event_id=f"receivable:{receivable.receivable_id}",
                event_type=EventType.RECEIVABLE,
                direction=Direction.INFLOW,
                amount=receivable.amount,
                expected_date=expected_date,
                account_id=receivable.destination_account_id,
                certainty=certainty,
                is_essential=False,
                is_adjustable=False,
                source_reference_id=receivable.receivable_id,
                source=receivable.source.value,
            )
        )

    card_by_id = {card.card_id: card for card in snapshot.cards}
    for card in snapshot.cards:
        if card.current_billing_amount == 0:
            continue
        if _scheduled_match(
            events,
            source_reference_id=card.card_id,
            event_type=EventType.CARD_BILL,
            expected_date=card.billing_date,
            account_id=card.payment_account_id,
            amount=card.current_billing_amount,
        ):
            continue
        events.append(
            _CoreEvent(
                event_id=f"card-bill:{card.card_id}",
                event_type=EventType.CARD_BILL,
                direction=Direction.OUTFLOW,
                amount=card.current_billing_amount,
                expected_date=card.billing_date,
                account_id=card.payment_account_id,
                certainty=Certainty.CONFIRMED,
                is_essential=True,
                is_adjustable=False,
                source_reference_id=card.card_id,
                source=InputSource.SYSTEM_DERIVED.value,
            )
        )

    for plan in snapshot.installment_plans:
        if plan.status != InstallmentStatus.ACTIVE or plan.remaining_months == 0:
            continue
        card = card_by_id[plan.card_id]
        for payment_index in range(plan.remaining_months):
            payment_date = _add_months(plan.next_payment_date, payment_index)
            current_bill_includes_payment = (
                payment_index == 0
                and card.current_billing_amount > 0
                and card.billing_date == payment_date
            )
            if current_bill_includes_payment or _scheduled_match(
                events,
                source_reference_id=plan.installment_plan_id,
                event_type=EventType.INSTALLMENT_PAYMENT,
                expected_date=payment_date,
                account_id=card.payment_account_id,
                amount=plan.monthly_payment,
            ):
                continue
            events.append(
                _CoreEvent(
                    event_id=f"installment:{plan.installment_plan_id}:{payment_index + 1}",
                    event_type=EventType.INSTALLMENT_PAYMENT,
                    direction=Direction.OUTFLOW,
                    amount=plan.monthly_payment,
                    expected_date=payment_date,
                    account_id=card.payment_account_id,
                    certainty=Certainty.CONFIRMED,
                    is_essential=True,
                    is_adjustable=False,
                    source_reference_id=plan.installment_plan_id,
                    source=InputSource.SYSTEM_DERIVED.value,
                )
            )
    return tuple(events)


def _event_priority(event: _CoreEvent) -> tuple[int, str]:
    if event.source_reference_id and event.source_reference_id.startswith("virtual-transfer:"):
        return (1, event.event_id)
    if event.direction == Direction.INFLOW and event.certainty == Certainty.CONFIRMED:
        return (0, event.event_id)
    if event.direction == Direction.INFLOW:
        return (0, event.event_id)
    if event.is_essential and event.event_type not in {
        EventType.CARD_BILL,
        EventType.INSTALLMENT_PAYMENT,
    }:
        return (2, event.event_id)
    if event.event_type in {EventType.CARD_BILL, EventType.INSTALLMENT_PAYMENT}:
        return (3, event.event_id)
    if event.event_type == EventType.SAVINGS:
        return (5, event.event_id)
    return (4, event.event_id)


def _events_for_scenario(
    snapshot: FinancialSnapshot,
    events: tuple[_CoreEvent, ...],
    scenario: DelayScenario,
) -> dict[date, list[_CoreEvent]]:
    delay_days = DELAY_SCENARIO_DAYS[scenario.value]
    receivable_status = {
        receivable.receivable_id: receivable.status for receivable in snapshot.receivables
    }
    by_date: dict[date, list[_CoreEvent]] = {}
    for event in events:
        event_date = event.expected_date
        linked_status = (
            receivable_status.get(event.source_reference_id)
            if event.source_reference_id is not None
            else None
        )
        delayable_receivable = event.event_type == EventType.RECEIVABLE and (
            event.certainty != Certainty.CONFIRMED
            or linked_status in {ReceivableStatus.ESTIMATED, ReceivableStatus.OVERDUE}
        )
        if delayable_receivable:
            event_date += timedelta(days=delay_days)
        by_date.setdefault(event_date, []).append(event)
    for dated_events in by_date.values():
        dated_events.sort(key=_event_priority)
    return by_date


def _available_for_account(
    account: Account,
    balance: int,
    protected_amount: int,
) -> int:
    return max(0, balance - protected_amount - account.minimum_balance)


def _pooled_available_balance(
    snapshot: FinancialSnapshot,
    accounts: dict[str, Account],
    balances: dict[str, int],
    protected: dict[str, int],
) -> int:
    """Return net liquidity so one positive account cannot cover a deficit twice."""

    return max(0, _liquidity_margin(snapshot, accounts, balances, protected))


def _liquidity_margin(
    snapshot: FinancialSnapshot,
    accounts: dict[str, Account],
    balances: dict[str, int],
    protected: dict[str, int],
) -> int:
    """Return the signed margin after every configured liquidity floor."""

    net_after_account_floors = sum(
        balances[account_id] - protected[account_id] - account.minimum_balance
        for account_id, account in accounts.items()
    )
    return net_after_account_floors - snapshot.preferences.minimum_total_reserve


def _payment_account_margin(
    snapshot: FinancialSnapshot,
    accounts: dict[str, Account],
    balances: dict[str, int],
    protected: dict[str, int],
    payment_account_ids: set[str],
) -> int:
    """Return the smallest signed safety margin among accounts used for outflows."""

    relevant_ids = payment_account_ids or {
        account_id for account_id, account in accounts.items() if account.is_payment_account
    }
    if not relevant_ids:
        return _liquidity_margin(snapshot, accounts, balances, protected)
    return min(
        balances[account_id] - protected[account_id] - accounts[account_id].minimum_balance
        for account_id in relevant_ids
    )


def _simulate_scenario(
    snapshot: FinancialSnapshot,
    events: tuple[_CoreEvent, ...],
    scenario: DelayScenario,
) -> ScenarioCashflow:
    start_date = snapshot.as_of.date()
    accounts = {account.account_id: account for account in snapshot.accounts}
    balances = {account.account_id: account.balance for account in snapshot.accounts}
    protected = _protected_by_account(snapshot)
    events_by_date = _events_for_scenario(snapshot, events, scenario)
    end_date = start_date + timedelta(days=ANALYSIS_HORIZON_DAYS)
    payment_account_ids = (
        {account.account_id for account in snapshot.accounts if account.is_payment_account}
        | {card.payment_account_id for card in snapshot.cards}
        | {
            event.account_id
            for event in events
            if event.direction == Direction.OUTFLOW and start_date <= event.expected_date < end_date
        }
    )

    positions: list[DailyPosition] = []
    shortfalls: list[ShortfallIncident] = []
    for offset in range(ANALYSIS_HORIZON_DAYS):
        current_date = start_date + timedelta(days=offset)
        triggering_event_ids: list[str] = []
        for event in events_by_date.get(current_date, []):
            triggering_event_ids.append(event.event_id)
            if event.direction == Direction.INFLOW:
                balances[event.account_id] += event.amount
                continue

            target_account = accounts[event.account_id]
            target_available = _available_for_account(
                target_account,
                balances[event.account_id],
                protected[event.account_id],
            )
            total_available = _pooled_available_balance(
                snapshot,
                accounts,
                balances,
                protected,
            )
            shortfall_type: ShortfallType | None = None
            gap = 0
            if total_available < event.amount:
                shortfall_type = ShortfallType.TOTAL_LIQUIDITY
                gap = event.amount - total_available
            elif target_available < event.amount:
                transfer_available = sum(
                    _available_for_account(account, balances[account_id], protected[account_id])
                    for account_id, account in accounts.items()
                    if account_id != event.account_id and account.is_available_for_transfer
                )
                gap = event.amount - target_available
                shortfall_type = (
                    ShortfallType.PAYMENT_ACCOUNT
                    if transfer_available >= gap
                    else ShortfallType.TOTAL_LIQUIDITY
                )
            if shortfall_type is not None:
                shortfalls.append(
                    ShortfallIncident(
                        date=current_date,
                        shortfall_type=shortfall_type,
                        gap=gap,
                        account_id=event.account_id,
                        event_id=event.event_id,
                        is_essential=event.is_essential,
                    )
                )
            balances[event.account_id] -= event.amount

        total_balance = sum(balances.values())
        protected_balance = sum(
            min(max(0, balances[account_id]), protected[account_id]) for account_id in accounts
        )
        positions.append(
            DailyPosition(
                date=current_date,
                account_balances=dict(balances),
                total_balance=total_balance,
                available_balance=_pooled_available_balance(
                    snapshot,
                    accounts,
                    balances,
                    protected,
                ),
                liquidity_margin=_liquidity_margin(
                    snapshot,
                    accounts,
                    balances,
                    protected,
                ),
                payment_account_margin=_payment_account_margin(
                    snapshot,
                    accounts,
                    balances,
                    protected,
                    payment_account_ids,
                ),
                protected_balance=protected_balance,
                status=RiskStatus.STABLE,
                triggering_event_ids=tuple(triggering_event_ids),
            )
        )

    return ScenarioCashflow(
        scenario=scenario,
        weight=DELAY_SCENARIO_WEIGHTS[scenario.value],
        daily_positions=tuple(positions),
        shortfalls=tuple(shortfalls),
    )


def _has_automatically_stale_source(snapshot: FinancialSnapshot) -> bool:
    stale_cutoff = snapshot.as_of - timedelta(days=STALE_SOURCE_DAYS)
    return any(
        updated_at < stale_cutoff
        for updated_at in (
            *(account.updated_at for account in snapshot.accounts),
            *(card.updated_at for card in snapshot.cards),
            *(counterparty.updated_at for counterparty in snapshot.counterparties),
            *(
                receivable.updated_at
                for receivable in snapshot.receivables
                if receivable.status not in {ReceivableStatus.RECEIVED, ReceivableStatus.CANCELLED}
            ),
        )
    )


def _data_confidence(snapshot: FinancialSnapshot) -> tuple[float, bool]:
    quality = snapshot.data_quality
    automatically_stale = _has_automatically_stale_source(snapshot)
    unconfirmed_receivables = sum(
        not receivable.user_confirmed
        and receivable.status in {ReceivableStatus.ESTIMATED, ReceivableStatus.OVERDUE}
        for receivable in snapshot.receivables
    )
    confidence = 1.0
    confidence -= DATA_CONFIDENCE_MISSING_SOURCE_PENALTY * len(quality.missing_sources)
    confidence -= DATA_CONFIDENCE_STALE_SOURCE_PENALTY * (
        len(quality.stale_sources) + int(automatically_stale)
    )
    confidence -= DATA_CONFIDENCE_UNCONFIRMED_ITEM_PENALTY * (
        len(quality.unconfirmed_items) + unconfirmed_receivables
    )
    requires_verification = bool(
        quality.missing_sources
        or quality.stale_sources
        or quality.unconfirmed_items
        or automatically_stale
        or unconfirmed_receivables
    )
    return max(MINIMUM_DATA_CONFIDENCE, min(1.0, confidence)), requires_verification


def _daily_verification_scope(snapshot: FinancialSnapshot) -> tuple[bool, set[date]]:
    """Separate persistent source gaps from uncertainty tied to specific dates."""

    quality = snapshot.data_quality
    global_verification = bool(
        quality.missing_sources
        or quality.stale_sources
        or _has_automatically_stale_source(snapshot)
    )
    relevant_dates: set[date] = set()
    max_delay_days = max(DELAY_SCENARIO_DAYS.values())
    for receivable in snapshot.receivables:
        if receivable.status in {
            ReceivableStatus.RECEIVED,
            ReceivableStatus.CANCELLED,
        }:
            continue
        if (
            receivable.status
            not in {
                ReceivableStatus.ESTIMATED,
                ReceivableStatus.OVERDUE,
            }
            and receivable.user_confirmed
        ):
            continue
        first_date = max(snapshot.as_of.date(), receivable.expected_date)
        relevant_dates.update(
            first_date + timedelta(days=offset) for offset in range(max_delay_days + 1)
        )
    relevant_dates.update(
        event.expected_date
        for event in snapshot.scheduled_events
        if event.certainty == Certainty.ESTIMATED and event.expected_date >= snapshot.as_of.date()
    )
    return global_verification, relevant_dates


def _risk_metrics(
    snapshot: FinancialSnapshot,
    scenarios: tuple[ScenarioCashflow, ...],
) -> RiskMetrics:
    risky_scenarios = [scenario for scenario in scenarios if scenario.shortfalls]
    payment_probability = sum(
        scenario.weight
        for scenario in scenarios
        if any(
            incident.shortfall_type == ShortfallType.PAYMENT_ACCOUNT
            for incident in scenario.shortfalls
        )
    )
    total_probability = sum(
        scenario.weight
        for scenario in scenarios
        if any(
            incident.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
            for incident in scenario.shortfalls
        )
    )
    shortfall_probability = sum(scenario.weight for scenario in risky_scenarios)
    confidence, requires_verification = _data_confidence(snapshot)
    if not risky_scenarios:
        return RiskMetrics(
            shortfall_probability=0,
            payment_account_shortfall_probability=0,
            total_liquidity_shortfall_probability=0,
            first_risk_date=None,
            shortfall_type=None,
            expected_gap_min=0,
            expected_gap_max=0,
            days_until_risk=None,
            data_confidence=confidence,
            requires_verification=requires_verification,
        )

    first_risk_date = min(
        incident.date for scenario in risky_scenarios for incident in scenario.shortfalls
    )
    first_date_incidents = [
        incident
        for scenario in risky_scenarios
        for incident in scenario.shortfalls
        if incident.date == first_risk_date
    ]
    shortfall_type = (
        ShortfallType.TOTAL_LIQUIDITY
        if any(
            incident.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
            for incident in first_date_incidents
        )
        else ShortfallType.PAYMENT_ACCOUNT
    )
    first_incidents = [scenario.first_shortfall for scenario in risky_scenarios]
    first_incidents = [incident for incident in first_incidents if incident is not None]
    return RiskMetrics(
        shortfall_probability=min(1.0, shortfall_probability),
        payment_account_shortfall_probability=min(1.0, payment_probability),
        total_liquidity_shortfall_probability=min(1.0, total_probability),
        first_risk_date=first_risk_date,
        shortfall_type=shortfall_type,
        expected_gap_min=min(incident.gap for incident in first_incidents),
        expected_gap_max=max(incident.gap for incident in first_incidents),
        days_until_risk=(first_risk_date - snapshot.as_of.date()).days,
        data_confidence=confidence,
        has_essential_risk=any(incident.is_essential for incident in first_incidents),
        requires_verification=requires_verification,
        triggering_event_ids=tuple(
            sorted(
                {
                    incident.event_id
                    for incident in first_date_incidents
                    if incident.event_id is not None
                }
            )
        ),
    )


def _daily_statuses(
    snapshot: FinancialSnapshot,
    scenarios: tuple[ScenarioCashflow, ...],
    metrics: RiskMetrics,
) -> tuple[dict[date, RiskStatus], dict[date, tuple[str, ...]]]:
    """Derive an aggregate user state for each day across all delay scenarios."""

    statuses: dict[date, RiskStatus] = {}
    active_risk_event_ids: dict[date, tuple[str, ...]] = {}
    start_date = snapshot.as_of.date()
    accounts = {account.account_id: account for account in snapshot.accounts}
    protected = _protected_by_account(snapshot)
    global_verification, verification_dates = _daily_verification_scope(snapshot)
    unresolved_by_scenario: dict[DelayScenario, tuple[ShortfallIncident, ...]] = {
        scenario.scenario: () for scenario in scenarios
    }

    def remaining_incident(
        incident: ShortfallIncident,
        position: DailyPosition,
    ) -> ShortfallIncident | None:
        net_unprotected_balance = sum(
            position.account_balances[account_id] - protected[account_id] for account_id in accounts
        )
        total_gap = max(0, -net_unprotected_balance)
        if total_gap > 0:
            return incident.model_copy(
                update={
                    "date": position.date,
                    "shortfall_type": ShortfallType.TOTAL_LIQUIDITY,
                    "gap": total_gap,
                }
            )
        account_gap = max(
            0,
            protected[incident.account_id] - position.account_balances[incident.account_id],
        )
        if account_gap > 0:
            return incident.model_copy(
                update={
                    "date": position.date,
                    "shortfall_type": ShortfallType.PAYMENT_ACCOUNT,
                    "gap": account_gap,
                }
            )
        return None

    for offset in range(ANALYSIS_HORIZON_DAYS):
        current_date = start_date + timedelta(days=offset)
        requires_daily_verification = global_verification or current_date in verification_dates
        daily_confidence = metrics.data_confidence if requires_daily_verification else 1.0
        dated_scenarios: list[tuple[ScenarioCashflow, tuple[ShortfallIncident, ...]]] = []
        for scenario in scenarios:
            position = scenario.daily_positions[offset]
            carried = tuple(
                remaining
                for incident in unresolved_by_scenario[scenario.scenario]
                if (remaining := remaining_incident(incident, position)) is not None
            )
            current = tuple(
                incident for incident in scenario.shortfalls if incident.date == current_date
            )
            effective = (*carried, *current)
            dated_scenarios.append((scenario, effective))
            unresolved_by_scenario[scenario.scenario] = tuple(
                remaining
                for incident in effective
                if (remaining := remaining_incident(incident, position)) is not None
            )
        risky_scenarios = [item for item in dated_scenarios if item[1]]
        if not risky_scenarios:
            active_risk_event_ids[current_date] = ()
            daily_metrics = RiskMetrics(
                shortfall_probability=0,
                payment_account_shortfall_probability=0,
                total_liquidity_shortfall_probability=0,
                first_risk_date=None,
                shortfall_type=None,
                expected_gap_min=0,
                expected_gap_max=0,
                days_until_risk=None,
                data_confidence=daily_confidence,
                requires_verification=requires_daily_verification,
            )
        else:
            incidents = [
                incident
                for _, scenario_incidents in risky_scenarios
                for incident in scenario_incidents
            ]
            active_risk_event_ids[current_date] = tuple(
                sorted(
                    {incident.event_id for incident in incidents if incident.event_id is not None}
                )
            )
            has_total_liquidity_risk = any(
                incident.shortfall_type == ShortfallType.TOTAL_LIQUIDITY for incident in incidents
            )
            daily_metrics = RiskMetrics(
                shortfall_probability=min(
                    1.0, sum(scenario.weight for scenario, _ in risky_scenarios)
                ),
                payment_account_shortfall_probability=sum(
                    scenario.weight
                    for scenario, scenario_incidents in risky_scenarios
                    if any(
                        incident.shortfall_type == ShortfallType.PAYMENT_ACCOUNT
                        for incident in scenario_incidents
                    )
                ),
                total_liquidity_shortfall_probability=sum(
                    scenario.weight
                    for scenario, scenario_incidents in risky_scenarios
                    if any(
                        incident.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
                        for incident in scenario_incidents
                    )
                ),
                first_risk_date=current_date,
                shortfall_type=(
                    ShortfallType.TOTAL_LIQUIDITY
                    if has_total_liquidity_risk
                    else ShortfallType.PAYMENT_ACCOUNT
                ),
                expected_gap_min=min(incident.gap for incident in incidents),
                expected_gap_max=max(incident.gap for incident in incidents),
                days_until_risk=0,
                data_confidence=daily_confidence,
                has_essential_risk=any(incident.is_essential for incident in incidents),
                requires_verification=requires_daily_verification,
                triggering_event_ids=tuple(
                    sorted(
                        {
                            incident.event_id
                            for incident in incidents
                            if incident.event_id is not None
                        }
                    )
                ),
            )
        statuses[current_date] = select_risk_status(daily_metrics)
    return statuses, active_risk_event_ids


def _with_daily_statuses(
    snapshot: FinancialSnapshot,
    scenarios: tuple[ScenarioCashflow, ...],
    metrics: RiskMetrics,
) -> tuple[ScenarioCashflow, ...]:
    statuses, active_risk_event_ids = _daily_statuses(snapshot, scenarios, metrics)
    return tuple(
        scenario.model_copy(
            update={
                "daily_positions": tuple(
                    position.model_copy(
                        update={
                            "status": statuses[position.date],
                            "triggering_event_ids": tuple(
                                dict.fromkeys(
                                    (
                                        *position.triggering_event_ids,
                                        *active_risk_event_ids[position.date],
                                    )
                                )
                            ),
                        }
                    )
                    for position in scenario.daily_positions
                )
            }
        )
        for scenario in scenarios
    )


def simulate_cashflow(
    snapshot: FinancialSnapshot,
    *,
    seed: int = DEFAULT_SIMULATION_SEED,
) -> CashflowAnalysis:
    """Simulate all four required receivable-delay scenarios over exactly 91 days."""

    if abs(sum(DELAY_SCENARIO_WEIGHTS.values()) - 1.0) > 1e-9:
        raise ValueError("delay scenario weights must sum to 1")
    events = _normalized_events(snapshot)
    scenarios = tuple(_simulate_scenario(snapshot, events, scenario) for scenario in DelayScenario)
    risk_metrics = _risk_metrics(snapshot, scenarios)
    scenarios = _with_daily_statuses(snapshot, scenarios, risk_metrics)
    on_time = next(scenario for scenario in scenarios if scenario.scenario == DelayScenario.ON_TIME)
    return CashflowAnalysis(
        snapshot_id=snapshot.snapshot_id,
        analysis_horizon_days=ANALYSIS_HORIZON_DAYS,
        daily_positions=on_time.daily_positions,
        scenarios=scenarios,
        risk_metrics=risk_metrics,
        seed=seed,
        tool_version=CASHFLOW_TOOL_VERSION,
        model_version=DELAY_MODEL_VERSION,
    )


def _snapshot_with_extra_spend(
    snapshot: FinancialSnapshot,
    amount: int,
) -> FinancialSnapshot:
    if amount == 0:
        return snapshot
    account = next(
        (candidate for candidate in snapshot.accounts if candidate.is_payment_account),
        snapshot.accounts[0],
    )
    event = ScheduledCashEvent(
        event_id="safe-to-spend:probe",
        event_type=EventType.DISCRETIONARY_EXPENSE,
        direction=Direction.OUTFLOW,
        amount=amount,
        expected_date=snapshot.as_of.date(),
        account_id=account.account_id,
        certainty=Certainty.CONFIRMED,
        is_essential=False,
        is_adjustable=False,
        source=InputSource.SYSTEM_DERIVED,
        source_reference_id="safe-to-spend:probe",
    )
    return snapshot.model_copy(update={"scheduled_events": (*snapshot.scheduled_events, event)})


def _meets_protection_level(analysis: CashflowAnalysis, protection_level: float) -> bool:
    permitted_probability = 1.0 - protection_level
    return (
        analysis.risk_metrics.total_liquidity_shortfall_probability <= permitted_probability + 1e-9
    )


def _total_liquidity_binding_constraint(
    analysis: CashflowAnalysis,
    fallback_date: date,
) -> BindingConstraint:
    incidents = sorted(
        (
            incident
            for scenario in analysis.scenarios
            for incident in scenario.shortfalls
            if incident.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
        ),
        key=lambda incident: (
            incident.date,
            incident.event_id or "",
            incident.account_id,
        ),
    )
    if incidents:
        incident = incidents[0]
        return BindingConstraint(
            date=incident.date,
            event_id=incident.event_id,
            reason=ShortfallType.TOTAL_LIQUIDITY.value,
        )
    return BindingConstraint(
        date=fallback_date,
        event_id=None,
        reason="PROTECTION_LEVEL",
    )


def calculate_safe_to_spend(
    snapshot: FinancialSnapshot,
    *,
    protection_level: float | None = None,
    seed: int = DEFAULT_SIMULATION_SEED,
) -> SafeToSpendResult:
    """Find the largest integer-Won discretionary spend meeting the protection level."""

    selected_level = (
        snapshot.preferences.protection_level if protection_level is None else protection_level
    )
    if not 0 <= selected_level <= 1:
        raise ValueError("protection_level must be between 0 and 1")

    baseline = simulate_cashflow(snapshot, seed=seed)
    protected = _protected_by_account(snapshot)
    maximum = max(
        0,
        sum(
            _available_for_account(account, account.balance, protected[account.account_id])
            for account in snapshot.accounts
        )
        - snapshot.preferences.minimum_total_reserve,
    )
    if not _meets_protection_level(baseline, selected_level):
        return SafeToSpendResult(
            snapshot_id=snapshot.snapshot_id,
            safe_to_spend=0,
            protection_level=selected_level,
            binding_constraint=_total_liquidity_binding_constraint(
                baseline,
                snapshot.as_of.date(),
            ),
            data_confidence=baseline.risk_metrics.data_confidence,
            seed=seed,
            tool_version=SAFE_TO_SPEND_TOOL_VERSION,
        )

    low = 0
    high = maximum
    while low < high:
        candidate = (low + high + 1) // 2
        analysis = simulate_cashflow(
            _snapshot_with_extra_spend(snapshot, candidate),
            seed=seed,
        )
        if _meets_protection_level(analysis, selected_level):
            low = candidate
        else:
            high = candidate - 1

    binding_constraint: BindingConstraint | None = None
    if low < maximum:
        failing = simulate_cashflow(
            _snapshot_with_extra_spend(snapshot, low + 1),
            seed=seed,
        )
        binding_constraint = _total_liquidity_binding_constraint(
            failing,
            snapshot.as_of.date(),
        )
    return SafeToSpendResult(
        snapshot_id=snapshot.snapshot_id,
        safe_to_spend=low,
        protection_level=selected_level,
        binding_constraint=binding_constraint,
        data_confidence=baseline.risk_metrics.data_confidence,
        seed=seed,
        tool_version=SAFE_TO_SPEND_TOOL_VERSION,
    )
