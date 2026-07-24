from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from flowguard.core import (
    apply_actions_virtual,
    calculate_safe_to_spend,
    evaluate_action_plan,
    get_counterparty_evidence,
    map_risk_presentation,
    simulate_cashflow,
    validate_financial_policy,
)
from flowguard.domain import (
    Account,
    Card,
    Certainty,
    Counterparty,
    DataQuality,
    Direction,
    EventType,
    FinancialSnapshot,
    FundType,
    InstallmentPlan,
    PaymentHistory,
    ProtectedFund,
    Receivable,
    ReceivableStatus,
    RecentTrend,
    RiskStatus,
    ScheduledCashEvent,
    ShortfallType,
    Transaction,
)

AS_OF = datetime(2026, 8, 17, 9, tzinfo=ZoneInfo("Asia/Seoul"))


def account(
    account_id: str,
    balance: int,
    *,
    minimum_balance: int = 0,
    protected_amount: int = 0,
    payment: bool = False,
    transferable: bool = True,
) -> Account:
    return Account(
        account_id=account_id,
        name=account_id,
        account_type="CHECKING",
        balance=balance,
        minimum_balance=minimum_balance,
        protected_amount=protected_amount,
        is_payment_account=payment,
        is_available_for_transfer=transferable,
        updated_at=AS_OF,
    )


def event(
    event_id: str,
    event_type: EventType,
    direction: Direction,
    amount: int,
    expected_date: date,
    account_id: str,
    *,
    essential: bool = False,
    adjustable: bool = False,
    certainty: Certainty = Certainty.CONFIRMED,
    source_reference_id: str | None = None,
) -> ScheduledCashEvent:
    return ScheduledCashEvent(
        event_id=event_id,
        event_type=event_type,
        direction=direction,
        amount=amount,
        expected_date=expected_date,
        account_id=account_id,
        certainty=certainty,
        is_essential=essential,
        is_adjustable=adjustable,
        source_reference_id=source_reference_id,
    )


def snapshot(
    *,
    accounts: list[Account],
    scheduled_events: list[ScheduledCashEvent] | None = None,
    **updates: object,
) -> FinancialSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "snapshot-1",
        "user_id": "user-1",
        "as_of": AS_OF,
        "accounts": accounts,
        "scheduled_events": scheduled_events or [],
    }
    values.update(updates)
    return FinancialSnapshot.model_validate(values)


def test_scenario_1_stable_and_safe_to_spend_is_positive() -> None:
    stable = snapshot(
        accounts=[account("payment", 1_000_000, minimum_balance=100_000, payment=True)],
        scheduled_events=[
            event(
                "rent",
                EventType.RENT,
                Direction.OUTFLOW,
                200_000,
                date(2026, 8, 25),
                "payment",
                essential=True,
            )
        ],
    )

    analysis = simulate_cashflow(stable)
    safe = calculate_safe_to_spend(stable)
    presentation = map_risk_presentation(analysis)
    policy = validate_financial_policy(stable, [])

    assert len(analysis.daily_positions) == 91
    assert analysis.risk_metrics.shortfall_probability == 0
    assert analysis.risk_metrics.shortfall_type is None
    assert safe.safe_to_spend == 700_000
    assert presentation.status == RiskStatus.STABLE
    assert policy.valid
    assert not policy.violations


def test_scenario_2_payment_account_shortfall_and_transfer_resolution() -> None:
    payment_shortfall = snapshot(
        accounts=[
            account("payment", 100_000, payment=True),
            account("savings", 500_000),
        ],
        scheduled_events=[
            event(
                "card-bill",
                EventType.CARD_BILL,
                Direction.OUTFLOW,
                300_000,
                date(2026, 8, 25),
                "payment",
                essential=True,
            )
        ],
    )
    before = simulate_cashflow(payment_shortfall)
    safe = calculate_safe_to_spend(payment_shortfall)
    action = {
        "type": "transfer",
        "parameters": {
            "from_account_id": "savings",
            "to_account_id": "payment",
            "amount": 250_000,
            "execution_date": "2026-08-25",
        },
    }

    evaluation = evaluate_action_plan(payment_shortfall, [action])

    assert before.risk_metrics.shortfall_type == ShortfallType.PAYMENT_ACCOUNT
    assert before.risk_metrics.total_liquidity_shortfall_probability == 0
    assert safe.safe_to_spend == 300_000
    assert safe.binding_constraint is not None
    assert safe.binding_constraint.reason == ShortfallType.TOTAL_LIQUIDITY
    assert evaluation.valid
    assert evaluation.after.risk_metrics.shortfall_probability == 0
    assert all(
        before_position.total_balance == after_position.total_balance
        for before_position, after_position in zip(
            before.daily_positions,
            evaluation.after.daily_positions,
            strict=True,
        )
    )
    transfer_day = evaluation.after.daily_positions[8]
    assert transfer_day.triggering_event_ids[:2] == (
        "virtual-transfer:action-1:0-out",
        "virtual-transfer:action-1:1-in",
    )
    assert transfer_day.total_balance == 300_000


def test_scenario_3_total_liquidity_shortfall_rejects_protected_fund_transfer() -> None:
    total_shortfall = snapshot(
        accounts=[
            account("payment", 100_000, payment=True),
            account("tax", 1_000_000, protected_amount=1_000_000),
        ],
        protected_funds=[
            ProtectedFund(
                protected_fund_id="tax-reserve",
                account_id="tax",
                fund_type=FundType.TAX_RESERVE,
                amount=1_000_000,
                user_confirmed=True,
            )
        ],
        scheduled_events=[
            event(
                "rent",
                EventType.RENT,
                Direction.OUTFLOW,
                300_000,
                date(2026, 8, 25),
                "payment",
                essential=True,
            )
        ],
    )
    analysis = simulate_cashflow(total_shortfall)
    protected_transfer = {
        "type": "transfer",
        "parameters": {
            "from_account_id": "tax",
            "to_account_id": "payment",
            "amount": 200_000,
            "execution_date": "2026-08-24",
        },
    }
    evaluation = evaluate_action_plan(total_shortfall, [protected_transfer])

    assert analysis.risk_metrics.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
    assert analysis.daily_positions[0].available_balance == 100_000
    assert not evaluation.valid
    assert any(
        violation.code == "PROTECTED_FUND_VIOLATION" for violation in evaluation.policy_violations
    )


def test_scenario_4_delayed_receivable_exposes_card_risk_and_evidence() -> None:
    client = Counterparty(
        counterparty_id="client-b",
        name="거래처 B",
        is_recurring=True,
        payment_history_count=4,
        updated_at=AS_OF,
    )
    delayed_income = snapshot(
        accounts=[account("payment", 0, payment=True)],
        counterparties=[client],
        payment_histories=[
            PaymentHistory(
                payment_history_id="history-1",
                counterparty_id="client-b",
                expected_date=date(2026, 5, 1),
                actual_date=date(2026, 5, 1),
                amount=300_000,
            ),
            PaymentHistory(
                payment_history_id="history-2",
                counterparty_id="client-b",
                expected_date=date(2026, 6, 1),
                actual_date=date(2026, 6, 4),
                amount=300_000,
            ),
            PaymentHistory(
                payment_history_id="history-3",
                counterparty_id="client-b",
                expected_date=date(2026, 7, 1),
                actual_date=date(2026, 7, 8),
                amount=300_000,
            ),
            PaymentHistory(
                payment_history_id="history-4",
                counterparty_id="client-b",
                expected_date=date(2026, 8, 1),
                actual_date=date(2026, 8, 15),
                amount=300_000,
            ),
        ],
        receivables=[
            Receivable(
                receivable_id="receivable-1",
                counterparty_id="client-b",
                amount=300_000,
                expected_date=date(2026, 8, 20),
                status=ReceivableStatus.ESTIMATED,
                destination_account_id="payment",
                user_confirmed=True,
                updated_at=AS_OF,
            )
        ],
        scheduled_events=[
            event(
                "card-bill",
                EventType.CARD_BILL,
                Direction.OUTFLOW,
                250_000,
                date(2026, 8, 25),
                "payment",
                essential=True,
            )
        ],
    )

    analysis = simulate_cashflow(delayed_income)
    evidence = get_counterparty_evidence(delayed_income, "client-b")
    presentation = map_risk_presentation(analysis)

    assert analysis.risk_metrics.shortfall_probability == 0.5
    assert analysis.risk_metrics.first_risk_date == date(2026, 8, 25)
    assert analysis.risk_metrics.expected_gap_min == 250_000
    assert analysis.risk_metrics.expected_gap_max == 250_000
    assert evidence.payment_history_count == 4
    assert evidence.maximum_delay_days == 14
    assert evidence.recent_trend == RecentTrend.WORSENING
    assert presentation.status in {RiskStatus.PREPARE, RiskStatus.ACT_NOW}


def test_scenario_5_installment_concentration_has_no_double_counting() -> None:
    card = Card(
        card_id="card-1",
        name="신용카드",
        payment_account_id="payment",
        payment_day=25,
        current_billing_amount=250_000,
        billing_date=date(2026, 8, 25),
        updated_at=AS_OF,
    )
    concentrated = snapshot(
        accounts=[account("payment", 400_000, payment=True)],
        cards=[card],
        transactions=[
            Transaction(
                transaction_id="original-purchase",
                account_id="payment",
                occurred_at=datetime(
                    2026,
                    7,
                    1,
                    10,
                    tzinfo=ZoneInfo("Asia/Seoul"),
                ),
                direction=Direction.OUTFLOW,
                amount=1_500_000,
                transaction_type="PURCHASE",
                card_id="card-1",
            )
        ],
        installment_plans=[
            InstallmentPlan(
                installment_plan_id="installment-1",
                card_id="card-1",
                original_amount=600_000,
                monthly_payment=100_000,
                total_months=6,
                remaining_months=2,
                next_payment_date=date(2026, 8, 25),
            )
        ],
        scheduled_events=[
            event(
                "rent",
                EventType.RENT,
                Direction.OUTFLOW,
                200_000,
                date(2026, 8, 24),
                "payment",
                essential=True,
            )
        ],
    )

    analysis = simulate_cashflow(concentrated)
    august_25 = analysis.daily_positions[8]
    all_trigger_ids = {
        trigger_id
        for position in analysis.daily_positions
        for trigger_id in position.triggering_event_ids
    }

    assert analysis.risk_metrics.first_risk_date == date(2026, 8, 25)
    assert august_25.total_balance == -50_000
    assert august_25.triggering_event_ids == ("card-bill:card-1",)
    assert "installment:installment-1:1" not in all_trigger_ids
    assert "installment:installment-1:2" in all_trigger_ids
    assert "original-purchase" not in all_trigger_ids


def test_scenario_6_shifted_card_bill_creates_rebound_total_liquidity_risk() -> None:
    rebound = snapshot(
        accounts=[
            account("payment", 100_000, payment=True),
            account("rent-account", 300_000),
        ],
        scheduled_events=[
            event(
                "card-bill",
                EventType.CARD_BILL,
                Direction.OUTFLOW,
                300_000,
                date(2026, 8, 20),
                "payment",
                essential=True,
                adjustable=True,
            ),
            event(
                "rent",
                EventType.RENT,
                Direction.OUTFLOW,
                300_000,
                date(2026, 9, 1),
                "rent-account",
                essential=True,
            ),
        ],
    )
    shift = {
        "type": "shift_payment_date",
        "parameters": {
            "event_id": "card-bill",
            "from_date": "2026-08-20",
            "to_date": "2026-09-01",
        },
    }

    evaluation = evaluate_action_plan(rebound, [shift])

    assert evaluation.before.risk_metrics.shortfall_type == ShortfallType.PAYMENT_ACCOUNT
    assert evaluation.after.risk_metrics.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
    assert evaluation.risk_shift.detected
    assert evaluation.risk_shift.target_date == date(2026, 9, 1)
    assert not evaluation.valid


def test_determinism_minimum_total_reserve_and_failure_are_fail_closed() -> None:
    constrained = snapshot(
        accounts=[account("payment", 500_000, payment=True)],
        preferences={"protection_level": 0.9, "minimum_total_reserve": 300_000},
        scheduled_events=[
            event(
                "essential",
                EventType.INSURANCE,
                Direction.OUTFLOW,
                250_000,
                date(2026, 8, 18),
                "payment",
                essential=True,
            )
        ],
    )

    first = simulate_cashflow(constrained, seed=42)
    second = simulate_cashflow(constrained, seed=42)

    assert first.model_dump() == second.model_dump()
    assert first.risk_metrics.shortfall_type == ShortfallType.TOTAL_LIQUIDITY
    assert first.risk_metrics.expected_gap_min == 50_000

    invalid_event = event(
        "invalid",
        EventType.RENT,
        Direction.OUTFLOW,
        1,
        date(2026, 8, 18),
        "missing-account",
        essential=True,
    )
    invalid_snapshot = constrained.model_copy(update={"scheduled_events": (invalid_event,)})
    with pytest.raises(KeyError):
        simulate_cashflow(invalid_snapshot)


def test_account_and_protected_fund_views_of_same_money_are_not_double_counted() -> None:
    protected = snapshot(
        accounts=[
            account(
                "payment",
                1_000_000,
                protected_amount=400_000,
                payment=True,
            )
        ],
        protected_funds=[
            ProtectedFund(
                protected_fund_id="tax-reserve",
                account_id="payment",
                fund_type=FundType.TAX_RESERVE,
                amount=400_000,
                user_confirmed=True,
            )
        ],
    )

    analysis = simulate_cashflow(protected)
    safe = calculate_safe_to_spend(protected)

    assert analysis.daily_positions[0].protected_balance == 400_000
    assert analysis.daily_positions[0].available_balance == 600_000
    assert safe.safe_to_spend == 600_000


def test_snapshot_is_immutable_unknown_actions_fail_and_data_gaps_are_not_stable() -> None:
    uncertain = snapshot(
        accounts=[account("payment", 500_000, payment=True)],
        data_quality=DataQuality(missing_sources=("cards",)),
    )
    analysis = simulate_cashflow(uncertain)
    presentation = map_risk_presentation(analysis)

    assert presentation.status == RiskStatus.VERIFY
    with pytest.raises(ValidationError):
        evaluate_action_plan(uncertain, [{"type": "borrow_money", "parameters": {}}])
    with pytest.raises(ValidationError):
        uncertain.accounts[0].balance = 0


def test_virtual_reserve_and_purchase_actions_do_not_mutate_original_snapshot() -> None:
    original = snapshot(
        accounts=[account("payment", 500_000, payment=True)],
        scheduled_events=[
            event(
                "planned-purchase",
                EventType.DISCRETIONARY_EXPENSE,
                Direction.OUTFLOW,
                100_000,
                date(2026, 8, 20),
                "payment",
                adjustable=True,
                certainty=Certainty.ESTIMATED,
            )
        ],
    )
    actions = [
        {
            "type": "delay_purchase",
            "parameters": {
                "amount": 100_000,
                "from_date": "2026-08-20",
                "to_date": "2026-09-20",
            },
        },
        {
            "type": "reserve_funds",
            "parameters": {
                "account_id": "payment",
                "amount": 50_000,
                "fund_type": FundType.BUSINESS_RESERVE,
            },
        },
    ]

    virtual = apply_actions_virtual(original, actions)

    assert original.accounts[0].protected_amount == 0
    assert original.scheduled_events[0].expected_date == date(2026, 8, 20)
    assert virtual.accounts[0].protected_amount == 50_000
    assert virtual.scheduled_events[0].expected_date == date(2026, 9, 20)
    assert validate_financial_policy(original, actions).requires_user_approval


def test_confirm_receivable_is_valid_without_immediate_cashflow_mutation() -> None:
    needs_confirmation = snapshot(
        accounts=[account("payment", 0, payment=True)],
        receivables=[
            Receivable(
                receivable_id="receivable-1",
                counterparty_id="client-1",
                amount=100_000,
                expected_date=date(2026, 8, 20),
                status=ReceivableStatus.ESTIMATED,
                destination_account_id="payment",
                user_confirmed=True,
                updated_at=AS_OF,
            )
        ],
        scheduled_events=[
            event(
                "card-bill",
                EventType.CARD_BILL,
                Direction.OUTFLOW,
                150_000,
                date(2026, 8, 25),
                "payment",
                essential=True,
            )
        ],
    )
    action = {
        "type": "confirm_receivable",
        "parameters": {"receivable_id": "receivable-1"},
    }

    evaluation = evaluate_action_plan(needs_confirmation, [action])

    assert evaluation.valid
    assert evaluation.before.model_dump() == evaluation.after.model_dump()
    assert not evaluation.risk_shift.detected
    assert not evaluation.policy_violations


def test_ineffective_financial_action_is_not_a_valid_recommendation() -> None:
    payment_shortfall = snapshot(
        accounts=[
            account("payment", 100_000, payment=True),
            account("savings", 500_000),
            account("unrelated", 0),
        ],
        scheduled_events=[
            event(
                "card-bill",
                EventType.CARD_BILL,
                Direction.OUTFLOW,
                300_000,
                date(2026, 8, 25),
                "payment",
                essential=True,
            )
        ],
    )
    unrelated_transfer = {
        "type": "transfer",
        "parameters": {
            "from_account_id": "savings",
            "to_account_id": "unrelated",
            "amount": 100_000,
            "execution_date": "2026-08-20",
        },
    }

    evaluation = evaluate_action_plan(payment_shortfall, [unrelated_transfer])

    assert not evaluation.valid
    assert evaluation.before.risk_metrics == evaluation.after.risk_metrics
    assert any(violation.code == "ACTION_INEFFECTIVE" for violation in evaluation.policy_violations)
