from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from flowguard.core import apply_actions_virtual, evaluate_action_plan, validate_financial_policy
from flowguard.domain import (
    Account,
    Card,
    Certainty,
    Direction,
    EventType,
    FinancialSnapshot,
    Receivable,
    ReceivableStatus,
    ScheduledCashEvent,
)

AS_OF = datetime(2026, 8, 17, 9, tzinfo=ZoneInfo("Asia/Seoul"))


def _event(
    event_id: str,
    event_type: EventType,
    amount: int,
    expected_date: date,
    *,
    essential: bool = False,
    adjustable: bool = True,
    certainty: Certainty = Certainty.ESTIMATED,
) -> ScheduledCashEvent:
    return ScheduledCashEvent(
        event_id=event_id,
        event_type=event_type,
        direction=Direction.OUTFLOW,
        amount=amount,
        expected_date=expected_date,
        account_id="payment",
        certainty=certainty,
        is_essential=essential,
        is_adjustable=adjustable,
    )


def _snapshot(*, receivable_status: ReceivableStatus = ReceivableStatus.ESTIMATED):
    return FinancialSnapshot(
        snapshot_id="snapshot-actions",
        user_id="user-actions",
        as_of=AS_OF,
        accounts=(
            Account(
                account_id="payment",
                name="결제계좌",
                account_type="CHECKING",
                balance=1_000_000,
                is_payment_account=True,
                updated_at=AS_OF,
            ),
            Account(
                account_id="savings",
                name="저축계좌",
                account_type="SAVINGS",
                balance=500_000,
                updated_at=AS_OF,
            ),
        ),
        cards=(
            Card(
                card_id="card-1",
                name="업무 카드",
                payment_account_id="payment",
                payment_day=25,
                current_billing_amount=0,
                billing_date=date(2026, 8, 25),
                updated_at=AS_OF,
            ),
        ),
        receivables=(
            Receivable(
                receivable_id="receivable-1",
                counterparty_id="client-1",
                amount=100_000,
                expected_date=date(2026, 8, 30),
                status=receivable_status,
                destination_account_id="payment",
                user_confirmed=True,
                updated_at=AS_OF,
            ),
        ),
        scheduled_events=(
            _event(
                "planned-purchase",
                EventType.DISCRETIONARY_EXPENSE,
                100_000,
                date(2026, 8, 20),
            ),
            _event("monthly-saving", EventType.SAVINGS, 50_000, date(2026, 8, 21)),
            _event(
                "flexible-bill",
                EventType.OTHER_OUTFLOW,
                60_000,
                date(2026, 8, 22),
                essential=True,
                certainty=Certainty.CONFIRMED,
            ),
        ),
    )


def test_virtual_catalog_applies_budget_savings_date_and_installment_actions() -> None:
    virtual = apply_actions_virtual(
        _snapshot(),
        [
            {
                "type": "adjust_discretionary_budget",
                "parameters": {
                    "amount": 40_000,
                    "start_date": "2026-08-17",
                    "end_date": "2026-08-31",
                },
            },
            {
                "type": "pause_savings",
                "parameters": {
                    "event_ids": ["monthly-saving"],
                    "start_date": "2026-08-17",
                    "end_date": "2026-08-31",
                },
            },
            {
                "type": "shift_payment_date",
                "parameters": {
                    "event_id": "flexible-bill",
                    "from_date": "2026-08-22",
                    "to_date": "2026-09-22",
                },
            },
            {
                "type": "add_installment",
                "parameters": {
                    "purchase_amount": 100_001,
                    "installment_months": 3,
                    "first_payment_date": "2026-08-31",
                    "card_id": "card-1",
                },
            },
        ],
    )

    events = {item.event_id: item for item in virtual.scheduled_events}
    assert events["planned-purchase"].amount == 60_000
    assert "monthly-saving" not in events
    assert events["flexible-bill"].expected_date == date(2026, 9, 22)
    installment_events = [
        item
        for item in virtual.scheduled_events
        if item.event_id.startswith("virtual-installment:")
    ]
    assert [item.amount for item in installment_events] == [33_334, 33_334, 33_333]
    assert [item.expected_date for item in installment_events] == [
        date(2026, 8, 31),
        date(2026, 9, 30),
        date(2026, 10, 31),
    ]
    assert len(virtual.installment_plans) == 1


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (
            {
                "type": "transfer",
                "parameters": {
                    "from_account_id": "payment",
                    "to_account_id": "payment",
                    "amount": 1,
                    "execution_date": "2026-08-18",
                },
            },
            "different",
        ),
        (
            {
                "type": "transfer",
                "parameters": {
                    "from_account_id": "missing",
                    "to_account_id": "payment",
                    "amount": 1,
                    "execution_date": "2026-08-18",
                },
            },
            "unknown account",
        ),
        (
            {
                "type": "transfer",
                "parameters": {
                    "from_account_id": "savings",
                    "to_account_id": "payment",
                    "amount": 600_000,
                    "execution_date": "2026-08-18",
                },
            },
            "exceeds",
        ),
        (
            {
                "type": "reserve_funds",
                "parameters": {
                    "account_id": "missing",
                    "amount": 1,
                    "fund_type": "BUSINESS_RESERVE",
                },
            },
            "unknown account",
        ),
        (
            {
                "type": "reserve_funds",
                "parameters": {
                    "account_id": "savings",
                    "amount": 600_000,
                    "fund_type": "BUSINESS_RESERVE",
                },
            },
            "exceeds",
        ),
        (
            {
                "type": "adjust_discretionary_budget",
                "parameters": {
                    "amount": 200_000,
                    "start_date": "2026-08-17",
                    "end_date": "2026-08-31",
                },
            },
            "exceeds adjustable expenses",
        ),
        (
            {
                "type": "pause_savings",
                "parameters": {
                    "event_ids": ["planned-purchase"],
                    "start_date": "2026-08-17",
                    "end_date": "2026-08-31",
                },
            },
            "adjustable savings",
        ),
        (
            {
                "type": "pause_savings",
                "parameters": {
                    "event_ids": ["missing"],
                    "start_date": "2026-08-17",
                    "end_date": "2026-08-31",
                },
            },
            "unknown event",
        ),
        (
            {
                "type": "shift_payment_date",
                "parameters": {
                    "event_id": "flexible-bill",
                    "from_date": "2026-08-21",
                    "to_date": "2026-09-21",
                },
            },
            "not eligible",
        ),
        (
            {
                "type": "shift_payment_date",
                "parameters": {
                    "event_id": "missing",
                    "from_date": "2026-08-21",
                    "to_date": "2026-09-21",
                },
            },
            "unknown event",
        ),
        (
            {
                "type": "delay_purchase",
                "parameters": {
                    "amount": 99_999,
                    "from_date": "2026-08-20",
                    "to_date": "2026-09-20",
                },
            },
            "matching unconfirmed purchase",
        ),
        (
            {
                "type": "add_installment",
                "parameters": {
                    "purchase_amount": 100_000,
                    "installment_months": 3,
                    "first_payment_date": "2026-08-31",
                    "card_id": "missing",
                },
            },
            "unknown card",
        ),
        (
            {
                "type": "add_installment",
                "parameters": {
                    "purchase_amount": 1,
                    "installment_months": 2,
                    "first_payment_date": "2026-08-31",
                    "card_id": "card-1",
                },
            },
            "cannot exceed purchase amount",
        ),
        (
            {
                "type": "confirm_receivable",
                "parameters": {"receivable_id": "missing"},
            },
            "unknown receivable",
        ),
    ],
)
def test_virtual_catalog_fails_closed_for_invalid_actions(
    action: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        apply_actions_virtual(_snapshot(), [action])


def test_virtual_receivable_confirmation_rejects_closed_item() -> None:
    action = {
        "type": "confirm_receivable",
        "parameters": {"receivable_id": "receivable-1"},
    }

    with pytest.raises(ValueError, match="closed receivable"):
        apply_actions_virtual(_snapshot(receivable_status=ReceivableStatus.RECEIVED), [action])


def test_policy_reports_each_catalog_boundary_and_stale_inputs() -> None:
    actions = [
        {
            "action_id": "same-account",
            "type": "transfer",
            "parameters": {
                "from_account_id": "payment",
                "to_account_id": "payment",
                "amount": 1,
                "execution_date": "2025-01-01",
            },
        },
        {
            "action_id": "unknown-transfer",
            "type": "transfer",
            "parameters": {
                "from_account_id": "missing",
                "to_account_id": "payment",
                "amount": 1,
                "execution_date": "2026-08-18",
            },
        },
        {
            "action_id": "unknown-reserve",
            "type": "reserve_funds",
            "parameters": {
                "account_id": "missing",
                "amount": 1,
                "fund_type": "BUSINESS_RESERVE",
            },
        },
        {
            "action_id": "large-reserve",
            "type": "reserve_funds",
            "parameters": {
                "account_id": "savings",
                "amount": 600_000,
                "fund_type": "BUSINESS_RESERVE",
            },
        },
        {
            "action_id": "large-budget-cut",
            "type": "adjust_discretionary_budget",
            "parameters": {
                "amount": 200_000,
                "start_date": "2026-08-17",
                "end_date": "2026-08-31",
            },
        },
        {
            "action_id": "wrong-savings",
            "type": "pause_savings",
            "parameters": {
                "event_ids": ["planned-purchase"],
                "start_date": "2026-08-17",
                "end_date": "2026-08-31",
            },
        },
        {
            "action_id": "wrong-shift",
            "type": "shift_payment_date",
            "parameters": {
                "event_id": "flexible-bill",
                "from_date": "2026-08-21",
                "to_date": "2026-09-21",
            },
        },
        {
            "action_id": "late-shift",
            "type": "shift_payment_date",
            "parameters": {
                "event_id": "flexible-bill",
                "from_date": "2026-08-22",
                "to_date": "2027-01-01",
            },
        },
        {
            "action_id": "missing-purchase",
            "type": "delay_purchase",
            "parameters": {
                "amount": 99_999,
                "from_date": "2026-08-20",
                "to_date": "2026-09-20",
            },
        },
        {
            "action_id": "missing-card",
            "type": "add_installment",
            "parameters": {
                "purchase_amount": 100_000,
                "installment_months": 3,
                "first_payment_date": "2026-08-31",
                "card_id": "missing",
            },
        },
        {
            "action_id": "late-installment",
            "type": "add_installment",
            "parameters": {
                "purchase_amount": 100_000,
                "installment_months": 3,
                "first_payment_date": "2027-01-01",
                "card_id": "card-1",
            },
        },
        {
            "action_id": "closed-receivable",
            "type": "confirm_receivable",
            "parameters": {"receivable_id": "receivable-1"},
        },
    ]
    stale = _snapshot(receivable_status=ReceivableStatus.CANCELLED).model_copy(
        update={
            "accounts": tuple(
                item.model_copy(update={"updated_at": AS_OF - timedelta(days=100)})
                for item in _snapshot().accounts
            )
        }
    )

    policy = validate_financial_policy(stale, actions)

    assert not policy.valid
    assert {item.code for item in policy.violations} >= {
        "ACTION_NOT_FEASIBLE",
        "MINIMUM_BALANCE_VIOLATION",
        "ESSENTIAL_EXPENSE_PROTECTION",
        "SNAPSHOT_STALE",
    }
    assert {item.action_id for item in policy.violations} >= {
        "same-account",
        "unknown-transfer",
        "unknown-reserve",
        "large-reserve",
        "large-budget-cut",
        "wrong-savings",
        "wrong-shift",
        "late-shift",
        "missing-purchase",
        "missing-card",
        "late-installment",
        "closed-receivable",
    }


def test_evaluation_converts_unchecked_application_error_to_policy_violation() -> None:
    evaluation = evaluate_action_plan(
        _snapshot(),
        [
            {
                "type": "pause_savings",
                "parameters": {
                    "event_ids": ["monthly-saving"],
                    "start_date": "2026-09-01",
                    "end_date": "2026-09-30",
                },
            }
        ],
    )

    assert not evaluation.valid
    assert evaluation.policy_violations[0].code == "ACTION_NOT_FEASIBLE"


def test_empty_action_plan_is_explicitly_ineffective() -> None:
    evaluation = evaluate_action_plan(_snapshot(), [])

    assert not evaluation.valid
    assert evaluation.policy_violations[0].code == "ACTION_INEFFECTIVE"
