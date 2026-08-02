from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.services.reports import ReportQueryService
from flowguard.storage import FlowGuardRepository


@pytest.fixture
def api() -> Iterator[tuple[TestClient, FlowGuardRepository]]:
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        yield client, repository


def _create_account(
    client: TestClient,
    account_id: str,
    balance: int,
    *,
    payment: bool = False,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/accounts",
        json={
            "account_id": account_id,
            "name": account_id,
            "account_type": "CHECKING",
            "balance": balance,
            "is_payment_account": payment,
            "is_available_for_transfer": True,
        },
    )
    assert response.status_code == 201
    return response.json()


def test_weekly_timeline_aggregation_accepts_model_like_positions() -> None:
    position = SimpleNamespace(
        date="2026-07-24",
        available_balance=120_000,
        liquidity_margin=20_000,
        payment_account_margin=-30_000,
        status=SimpleNamespace(value="ACT_NOW"),
        triggering_event_ids=("card-bill", "card-bill"),
    )

    weekly = ReportQueryService._weekly_positions(
        {
            "daily_positions": [position],
            "scenarios": [SimpleNamespace(daily_positions=[position])],
        },
        margin_field="payment_account_margin",
        event_descriptions={"card-bill": "카드 결제대금 30,000원"},
    )

    assert weekly == [
        {
            "week": 1,
            "start_date": "2026-07-24",
            "end_date": "2026-07-24",
            "min_available_balance": 120_000,
            "min_safety_margin": -30_000,
            "status": "ACT_NOW",
            "causes": ["카드 결제대금 30,000원"],
        }
    ]


def test_legacy_timeline_uses_an_honest_balance_label() -> None:
    legacy_position = {"date": "2026-07-24", "available_balance": 120_000}

    contract = ReportQueryService._margin_contract(
        {
            "daily_positions": [legacy_position],
            "scenarios": [{"daily_positions": [legacy_position]}],
        },
        shortfall_type="PAYMENT_ACCOUNT",
    )

    assert contract == (
        "available_balance",
        "LEGACY_AVAILABLE_BALANCE",
        "전체 가용잔액 · 다시 분석 필요",
        True,
    )


def test_preferences_are_persisted_and_used_by_dashboard(
    api: tuple[TestClient, FlowGuardRepository],
) -> None:
    client, _ = api
    _create_account(client, "account-1", 1_000_000, payment=True)
    preferences = client.patch(
        "/api/v1/preferences",
        json={
            "protection_level": 0.95,
            "minimum_total_reserve": 100_000,
            "income_type": "PROJECT",
        },
    )
    assert preferences.status_code == 200

    analysis = client.post(
        "/api/v1/analyses",
        json={"as_of": "2026-07-24T09:00:00+09:00"},
    )
    assert analysis.status_code == 201
    assert analysis.json()["status"] == "COMPLETED"

    dashboard = client.get("/api/v1/dashboard")
    assert dashboard.status_code == 200
    payload = dashboard.json()
    assert payload["safe_to_spend"]["protection_level"] == 0.95
    assert payload["safe_to_spend"]["safe_to_spend"] == 900_000
    assert payload["analysis_status"] == "SUCCEEDED"
    assert "risk_metrics" in payload
    assert "data_quality" in payload

    report_tools = client.app.state.report_service.tools
    with patch.object(
        report_tools,
        "query_financial_events",
        wraps=report_tools.query_financial_events,
    ) as event_query:
        timeline = client.get("/api/v1/cashflow/timeline").json()
    event_query.assert_called_once()
    assert timeline["horizon_days"] == 91
    assert timeline["requires_reanalysis"] is False
    valid_statuses = {"STABLE", "VERIFY", "PREPARE", "ACT_NOW"}
    assert {position["status"] for position in timeline["daily_positions"]} <= valid_statuses
    assert all(
        {position["status"] for position in scenario["daily_positions"]} <= valid_statuses
        for scenario in timeline["scenarios"]
    )
    assert {scenario["scenario_label"] for scenario in timeline["scenarios"]} == {
        "기준",
        "3일 지연",
        "7일 지연",
        "14일 지연",
    }
    weekly_positions = timeline["weekly_positions"]
    assert [position["week"] for position in weekly_positions] == list(range(1, 14))
    assert all(position["causes"] == [] for position in weekly_positions)
    status_severity = {"STABLE": 0, "VERIFY": 1, "PREPARE": 2, "ACT_NOW": 3}
    margin_field = (
        "payment_account_margin"
        if payload["risk_metrics"]["shortfall_type"] == "PAYMENT_ACCOUNT"
        else "liquidity_margin"
    )
    for index, weekly in enumerate(weekly_positions):
        scenario_week = [
            position
            for scenario in timeline["scenarios"]
            for position in scenario["daily_positions"][index * 7 : (index + 1) * 7]
        ]
        assert weekly["start_date"] == timeline["daily_positions"][index * 7]["date"]
        assert weekly["end_date"] == timeline["daily_positions"][(index + 1) * 7 - 1]["date"]
        assert weekly["min_available_balance"] == min(
            position["available_balance"] for position in scenario_week
        )
        assert weekly["min_safety_margin"] == min(
            position[margin_field] for position in scenario_week
        )
        assert weekly["status"] == max(
            (position["status"] for position in scenario_week),
            key=status_severity.__getitem__,
        )


def test_carried_shortfall_keeps_its_cause_in_the_following_week(
    api: tuple[TestClient, FlowGuardRepository],
) -> None:
    client, _ = api
    _create_account(client, "payment", 100, payment=True)
    event = client.post(
        "/api/v1/scheduled-events",
        json={
            "event_id": "persistent-bill",
            "event_type": "CARD_BILL",
            "direction": "OUTFLOW",
            "amount": 200,
            "expected_date": "2026-07-25",
            "account_id": "payment",
            "certainty": "CONFIRMED",
            "is_essential": True,
        },
    )
    assert event.status_code == 201
    analysis = client.post(
        "/api/v1/analyses",
        json={"as_of": "2026-07-24T09:00:00+09:00"},
    )
    assert analysis.status_code == 201

    timeline = client.get("/api/v1/cashflow/timeline").json()
    second_week = timeline["weekly_positions"][1]

    assert second_week["status"] == "ACT_NOW"
    assert any("카드 결제대금" in cause for cause in second_week["causes"])


def test_wide_import_reports_counts_and_timestamp_quality_notice(
    api: tuple[TestClient, FlowGuardRepository],
) -> None:
    client, _ = api
    content = (
        "record_type,account_id,name,account_type,balance\n"
        "ACCOUNT,account-1,생활비,CHECKING,800000\n"
    )

    response = client.post(
        "/api/v1/imports/transactions",
        files={"file": ("financial.csv", content, "text/csv")},
        data={
            "protection_level": "0.9",
            "minimum_total_reserve": "100000",
            "income_type": "PROJECT",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["imported_count"] == 1
    assert payload["imported_counts"]["accounts"] == 1
    assert payload["revision"]
    assert payload["analysis_required"] is True
    assert any(notice["code"] == "MISSING_UPDATED_AT" for notice in payload["data_quality_notices"])


def test_structured_error_and_user_input_paths(
    api: tuple[TestClient, FlowGuardRepository],
) -> None:
    client, repository = api
    bad_csv = (
        "transaction_id,account_id,occurred_at,direction,amount,description,unknown\n"
        "t1,a,2026-01-01T09:00:00+09:00,INFLOW,100000,x,nope\n"
    )
    invalid = client.post(
        "/api/v1/imports/transactions",
        files={"file": ("bad.csv", bad_csv, "text/csv")},
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "code": "INVALID_CSV_FORMAT",
        "message": "지원하지 않는 CSV 컬럼이 있습니다.",
        "details": {"unknown_columns": ["unknown"]},
        "retryable": False,
    }

    _create_account(client, "account-1", 500_000, payment=True)
    counterparty = client.post(
        "/api/v1/counterparties",
        json={"counterparty_id": "client-1", "name": "디자인컴퍼니"},
    )
    assert counterparty.status_code == 201
    receivable = client.post(
        "/api/v1/receivables",
        json={
            "receivable_id": "receivable-1",
            "counterparty_id": "client-1",
            "amount": 900_000,
            "expected_date": "2026-07-30",
            "destination_account_id": "account-1",
        },
    )
    assert receivable.status_code == 201
    assert client.get("/api/v1/counterparties").json()[0]["name"] == "디자인컴퍼니"
    assert client.get("/api/v1/receivables").json()[0]["amount"] == 900_000

    repository.upsert_records(
        "demo-user",
        "candidates",
        [
            {
                "candidate_id": "candidate-1",
                "candidate_type": "RECURRING_INCOME",
                "status": "PENDING",
                "confidence": 0.8,
                "evidence_transaction_ids": [],
                "proposed_record": {},
            }
        ],
    )
    decision = client.patch(
        "/api/v1/candidates/candidate-1",
        json={
            "decision": "CONFIRMED",
            "details": {
                "counterparty_id": "client-1",
                "amount": 1_000_000,
                "expected_date": "2026-08-30",
                "destination_account_id": "account-1",
            },
        },
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "CONFIRMED"
    assert decision.json()["promotion"]["kind"] == "receivables"


def test_installment_precheck_uses_core_evaluation(
    api: tuple[TestClient, FlowGuardRepository],
) -> None:
    client, repository = api
    account = _create_account(client, "account-1", 1_000_000, payment=True)
    repository.upsert_records(
        "demo-user",
        "cards",
        [
            {
                "card_id": "card-1",
                "name": "업무 카드",
                "payment_account_id": "account-1",
                "payment_day": 25,
                "current_billing_amount": 0,
                "billing_date": "2026-07-25",
                "updated_at": account["updated_at"],
            }
        ],
    )
    analysis = client.post(
        "/api/v1/analyses",
        json={"as_of": "2026-07-24T09:00:00+09:00"},
    )
    assert analysis.json()["status"] == "COMPLETED"

    response = client.post(
        "/api/v1/installments/precheck",
        json={
            "purchase_amount": 120_000,
            "installment_months": 3,
            "first_payment_date": "2026-08-25",
            "card_id": "card-1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"]["type"] == "add_installment"
    assert payload["evaluation"]["tool_version"] == "action-evaluator-1"
    assert payload["policy"]["policy_version"] == "policy-1"
    assert payload["external_actions_executed"] is False


def test_approval_creates_full_virtual_report_without_mutating_current_records(
    api: tuple[TestClient, FlowGuardRepository],
) -> None:
    client, repository = api
    _create_account(client, "payment", 50_000, payment=True)
    _create_account(client, "reserve", 200_000)
    event = client.post(
        "/api/v1/scheduled-events",
        json={
            "event_id": "bill-1",
            "event_type": "CARD_BILL",
            "direction": "OUTFLOW",
            "amount": 100_000,
            "expected_date": "2026-07-25",
            "account_id": "payment",
            "certainty": "CONFIRMED",
            "is_essential": True,
        },
    )
    assert event.status_code == 201
    baseline = client.post(
        "/api/v1/analyses",
        json={"as_of": "2026-07-24T09:00:00+09:00"},
    )
    assert baseline.json()["status"] == "COMPLETED"
    recommendation = client.get("/api/v1/recommendations").json()[0]
    base_snapshot_id = recommendation["snapshot_id"]

    approved = client.post(
        f"/api/v1/recommendations/{recommendation['recommendation_id']}/approve",
        json={},
    )

    assert approved.status_code == 200
    payload = approved.json()
    assert payload["virtual_analysis"]["status"] == "COMPLETED"
    assert payload["virtual_report"]["is_virtual"] is True
    assert payload["virtual_report"]["base_snapshot_id"] == base_snapshot_id
    assert payload["virtual_report"]["snapshot_id"] != base_snapshot_id
    assert payload["virtual_application"]["external_actions_executed"] is False
    assert {
        item["account_id"]: item["balance"] for item in client.get("/api/v1/accounts").json()
    } == {"payment": 50_000, "reserve": 200_000}
    audit = repository.list_approval_audit("demo-user", recommendation["recommendation_id"])
    assert audit[0]["effect"] == "VIRTUAL_ONLY"
