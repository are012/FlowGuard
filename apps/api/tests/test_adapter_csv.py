from __future__ import annotations

import pytest

from flowguard.adapters import (
    CSVFinancialDataAdapter,
    CSVImportError,
    TransactionGroup,
)


def test_minimum_transaction_csv_is_normalized_with_explicit_quality_notice() -> None:
    csv_text = """transaction_id,account_id,occurred_at,direction,amount,description
txn-1,account-1,2026-01-01T09:00:00+09:00,INFLOW,1000000,프로젝트 대금
"""

    result = CSVFinancialDataAdapter().parse(csv_text)

    assert result.format == "MINIMUM_TRANSACTION"
    assert result.transactions == [
        {
            "transaction_id": "txn-1",
            "account_id": "account-1",
            "occurred_at": "2026-01-01T09:00:00+09:00",
            "direction": "INFLOW",
            "amount": 1_000_000,
            "description": "프로젝트 대금",
            "transaction_type": "OTHER",
            "source": "CSV",
            "is_internal_transfer": False,
        }
    ]
    codes = {notice["code"] for notice in result.data_quality_notices}
    assert "ACCOUNT_DETAILS_REQUIRED" in codes
    assert "INSUFFICIENT_HISTORY_FOR_CANDIDATES" in codes


def test_wide_csv_supports_each_record_type_without_inventing_missing_accounts() -> None:
    csv_text = (
        """record_type,account_id,name,account_type,balance,transaction_id,"""
        """occurred_at,direction,amount,description
ACCOUNT,account-1,생활비,CHECKING,800000,,,,,
TRANSACTION,account-1,,,,txn-1,2026-01-02T09:00:00+09:00,OUTFLOW,45000,마트
"""
    )

    result = CSVFinancialDataAdapter().parse(csv_text)

    assert result.format == "WIDE_RECORDS"
    assert result.accounts[0]["minimum_balance"] == 0
    assert result.accounts[0]["balance"] == 800_000
    assert result.transactions[0]["amount"] == 45_000
    assert all(
        notice["code"] != "ACCOUNT_DETAILS_REQUIRED" for notice in result.data_quality_notices
    )


def test_candidate_detection_is_deterministic_and_requires_confirmation() -> None:
    csv_text = (
        """transaction_id,account_id,occurred_at,direction,amount,description,"""
        """counterparty_name
txn-1,a,2026-01-01T09:00:00+09:00,INFLOW,1000000,프로젝트,디자인컴퍼니
txn-2,a,2026-02-01T09:00:00+09:00,INFLOW,990000,프로젝트,디자인컴퍼니
txn-3,a,2026-03-01T09:00:00+09:00,INFLOW,1010000,프로젝트,디자인컴퍼니
txn-4,a,2026-01-05T09:00:00+09:00,OUTFLOW,500000,월세,건물주
txn-5,a,2026-02-05T09:00:00+09:00,OUTFLOW,500000,월세,건물주
"""
    )

    first = CSVFinancialDataAdapter().parse(csv_text)
    second = CSVFinancialDataAdapter().parse(csv_text)

    assert first.candidates == second.candidates
    assert {item["candidate_type"] for item in first.candidates} == {
        "RECURRING_INCOME",
        "FIXED_EXPENSE",
    }
    assert all(item["status"] == "PENDING" for item in first.candidates)
    confirmation_ids = {
        notice["record_ids"][0]
        for notice in first.data_quality_notices
        if notice["code"] == "USER_CONFIRMATION_REQUIRED"
    }
    assert confirmation_ids == {item["candidate_id"] for item in first.candidates}


def test_validated_label_groups_feed_existing_deterministic_candidate_checks() -> None:
    csv_text = (
        "transaction_id,account_id,occurred_at,direction,amount,description,"
        "counterparty_name\n"
        "txn-1,a,2026-01-01T09:00:00+09:00,INFLOW,1000000,프로젝트,(주)디자인\n"
        "txn-2,a,2026-02-01T09:00:00+09:00,INFLOW,990000,프로젝트,디자인\n"
        "txn-3,a,2026-03-01T09:00:00+09:00,INFLOW,1010000,프로젝트,디자인(주)\n"
    )
    adapter = CSVFinancialDataAdapter()

    deterministic = adapter.parse(csv_text)
    classified = adapter.parse(
        csv_text,
        group_transactions=lambda transactions: [
            TransactionGroup(
                direction="INFLOW",
                key="G1",
                transactions=transactions,
                classification={
                    "source": "AI",
                    "group_id": "G1",
                    "normalized_name": "디자인",
                    "entity_kind": "CLIENT",
                    "category_hint": "RECEIVABLE",
                    "essential_hint": None,
                    "confidence": "HIGH",
                    "reason": "법인격 표기만 다릅니다.",
                    "labels": ["(주)디자인", "디자인", "디자인(주)"],
                    "can_split": True,
                },
            )
        ],
    )

    assert deterministic.candidates == []
    assert len(classified.candidates) == 1
    candidate = classified.candidates[0]
    assert candidate["candidate_type"] == "RECURRING_INCOME"
    assert candidate["proposed_record"]["counterparty_name"] == "디자인"
    assert candidate["classification_group"]["can_split"] is True


def test_deterministic_grouping_preserves_exact_label_fallback() -> None:
    transactions = [
        {
            "transaction_id": "one",
            "direction": "OUTFLOW",
            "counterparty_name": " 카드사 ",
        },
        {
            "transaction_id": "two",
            "direction": "OUTFLOW",
            "counterparty_name": "카드사",
        },
        {
            "transaction_id": "three",
            "direction": "OUTFLOW",
            "counterparty_name": "카드사 자동이체",
        },
    ]

    groups = CSVFinancialDataAdapter.deterministic_grouping(transactions)

    assert [group.key for group in groups] == ["카드사", "카드사 자동이체"]
    assert [[item["transaction_id"] for item in group.transactions] for group in groups] == [
        ["one", "two"],
        ["three"],
    ]


@pytest.mark.parametrize(
    ("csv_text", "detail_key"),
    [
        (
            "transaction_id,account_id,occurred_at,direction,amount,description,unknown\n"
            "txn-1,a,2026-01-01T09:00:00+09:00,INFLOW,100,x,y\n",
            "unknown_columns",
        ),
        (
            "transaction_id,account_id,occurred_at,direction,amount,description\n"
            "txn-1,a,2026-01-01T09:00:00+09:00,INFLOW,1,one\n"
            "txn-1,a,2026-01-01T09:00:00+09:00,INFLOW,2,two\n",
            "record_id",
        ),
    ],
)
def test_invalid_or_conflicting_csv_fails_closed(csv_text: str, detail_key: str) -> None:
    with pytest.raises(CSVImportError) as caught:
        CSVFinancialDataAdapter().parse(csv_text)

    assert caught.value.code == "INVALID_CSV_FORMAT"
    assert detail_key in caught.value.details


def test_wide_csv_rejects_value_in_field_for_another_record_type() -> None:
    csv_text = """record_type,account_id,name,account_type,balance,transaction_id
ACCOUNT,account-1,생활비,CHECKING,800000,txn-impossible
"""

    with pytest.raises(CSVImportError) as caught:
        CSVFinancialDataAdapter().parse(csv_text)

    assert caught.value.details["fields"] == ["transaction_id"]


def test_wide_csv_normalizes_payment_history_for_counterparty_evidence() -> None:
    csv_text = (
        "record_type,payment_history_id,counterparty_id,expected_date,"
        "actual_date,amount\n"
        "PAYMENT_HISTORY,payment-1,client-1,2026-01-10,2026-01-15,900000\n"
    )

    result = CSVFinancialDataAdapter().parse(csv_text)

    assert result.payment_histories == [
        {
            "payment_history_id": "payment-1",
            "counterparty_id": "client-1",
            "expected_date": "2026-01-10",
            "actual_date": "2026-01-15",
            "amount": 900_000,
        }
    ]


@pytest.mark.parametrize(
    "csv_text",
    [
        (
            "record_type,event_id,event_type,direction,amount,expected_date,account_id\n"
            "SCHEDULED_EVENT,e1,NOT_REAL,OUTFLOW,100,2026-01-01,a\n"
        ),
        (
            "record_type,receivable_id,counterparty_id,amount,expected_date,"
            "destination_account_id,status\n"
            "RECEIVABLE,r1,c1,100,2026-01-01,a,ACTIVE\n"
        ),
        (
            "record_type,installment_plan_id,card_id,original_amount,"
            "monthly_payment,total_months,remaining_months,next_payment_date,status\n"
            "INSTALLMENT_PLAN,i1,c1,100,10,10,5,2026-01-01,OVERDUE\n"
        ),
        (
            "record_type,account_id,name,account_type,balance,updated_at\n"
            "ACCOUNT,a,main,CHECKING,100,2026-01-01T09:00:00\n"
        ),
        (
            "record_type,installment_plan_id,card_id,original_amount,"
            "monthly_payment,total_months,remaining_months,next_payment_date\n"
            "INSTALLMENT_PLAN,i1,c1,0,0,0,0,2026-01-01\n"
        ),
        (
            "record_type,protected_fund_id,account_id,fund_type,amount\n"
            "PROTECTED_FUND,p1,a,TAX_RESERVE,0\n"
        ),
    ],
)
def test_wide_csv_rejects_values_that_domain_models_cannot_accept(
    csv_text: str,
) -> None:
    with pytest.raises(CSVImportError):
        CSVFinancialDataAdapter().parse(csv_text)
