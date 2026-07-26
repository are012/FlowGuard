from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.services.data import DataService
from flowguard.services.errors import ServiceError
from flowguard.services.snapshots import SnapshotBuilder
from flowguard.services.tools import CoreToolService
from flowguard.storage import FlowGuardRepository


def _client() -> tuple[TestClient, FlowGuardRepository]:
    repository = FlowGuardRepository("sqlite:///:memory:")
    return TestClient(create_app(repository), raise_server_exceptions=False), repository


def _post_account(
    client: TestClient,
    account_id: str,
    balance: int,
    *,
    payment: bool = False,
) -> None:
    response = client.post(
        "/api/v1/accounts",
        json={
            "account_id": account_id,
            "name": account_id,
            "account_type": "CHECKING",
            "balance": balance,
            "is_payment_account": payment,
        },
    )
    assert response.status_code == 201
    assert response.json()["revision"]
    assert response.json()["analysis_required"] is True


def _payment_risk(client: TestClient, *, second_source: bool = False) -> dict:
    _post_account(client, "payment", 50_000, payment=True)
    _post_account(client, "reserve-1", 100_000)
    if second_source:
        _post_account(client, "reserve-2", 100_000)
    event = client.post(
        "/api/v1/scheduled-events",
        json={
            "event_id": "bill",
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
    analysis = client.post(
        "/api/v1/analyses",
        json={
            "trigger_type": "DATA_REFRESH",
            "as_of": "2026-07-24T09:00:00+09:00",
        },
    )
    assert analysis.status_code == 201
    assert analysis.json()["status"] == "COMPLETED"
    recommendations = client.get("/api/v1/recommendations").json()
    assert len(recommendations) == 1
    return recommendations[0]


def test_live_revision_blocks_stale_approval_and_preserves_pending() -> None:
    client, repository = _client()
    with client:
        recommendation = _payment_risk(client)
        repository.patch_record("demo-user", "accounts", "reserve-1", {"balance": 80_000})

        response = client.post(
            f"/api/v1/recommendations/{recommendation['recommendation_id']}/approve",
            json={},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "SNAPSHOT_STALE"
        stored = repository.get_recommendation("demo-user", recommendation["recommendation_id"])
        assert stored["status"] == "PENDING"
        assert (
            repository.list_approval_audit("demo-user", recommendation["recommendation_id"]) == []
        )


def test_failed_virtual_analysis_never_records_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository = _client()
    with client:
        recommendation = _payment_risk(client)
        service = client.app.state.recommendation_service
        monkeypatch.setattr(
            service.analyses,
            "run_virtual",
            lambda *args, **kwargs: {
                "analysis_id": "analysis-failed",
                "status": "FAILED",
                "error": {"code": "ANALYSIS_FAILED"},
            },
        )

        response = client.post(
            f"/api/v1/recommendations/{recommendation['recommendation_id']}/approve",
            json={},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "ANALYSIS_FAILED"
        assert (
            repository.get_recommendation("demo-user", recommendation["recommendation_id"])[
                "status"
            ]
            == "PENDING"
        )
        assert (
            repository.list_approval_audit("demo-user", recommendation["recommendation_id"]) == []
        )


def test_alternatives_are_evaluated_and_final_recommendation_is_not_reoffered() -> None:
    client, _ = _client()
    with client:
        recommendation = _payment_risk(client, second_source=True)
        assert recommendation["alternatives"]

        alternatives = client.post(
            f"/api/v1/recommendations/{recommendation['recommendation_id']}/alternatives",
            json={},
        )
        assert alternatives.status_code == 200
        assert alternatives.json()["alternative_state"] == "AVAILABLE"

        rejected = client.post(
            f"/api/v1/recommendations/{recommendation['recommendation_id']}/reject",
            json={},
        )
        assert rejected.status_code == 200
        assert client.get("/api/v1/dashboard").json()["recommendation"] is None
        retry = client.post(
            f"/api/v1/recommendations/{recommendation['recommendation_id']}/alternatives",
            json={},
        )
        assert retry.status_code == 409


def test_import_replaces_notices_and_preserves_candidate_decision() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    service = DataService(repository)
    minimum = (
        "transaction_id,account_id,occurred_at,direction,amount,description\n"
        "t0,a,2025-12-01T09:00:00+09:00,INFLOW,1000000,프로젝트\n"
    )
    service.import_csv("user", minimum)
    assert any(
        item["code"] == "ACCOUNT_DETAILS_REQUIRED"
        for item in repository.list_records("user", "data_quality_notices")
    )
    wide = (
        "record_type,account_id,name,account_type,balance,updated_at,"
        "transaction_id,occurred_at,direction,amount,description,counterparty_name\n"
        "ACCOUNT,a,main,CHECKING,1000000,2026-07-24T09:00:00+09:00,,,,,,\n"
        "TRANSACTION,a,,,,,t1,2026-01-01T09:00:00+09:00,INFLOW,1000000,project,client\n"
        "TRANSACTION,a,,,,,t2,2026-02-01T09:00:00+09:00,INFLOW,1000000,project,client\n"
        "TRANSACTION,a,,,,,t3,2026-03-01T09:00:00+09:00,INFLOW,1000000,project,client\n"
    )
    imported = service.import_csv("user", wide)
    candidate_id = imported["candidates"][0]["candidate_id"]
    service.decide_candidate("user", candidate_id, decision="UNKNOWN")
    service.import_csv("user", wide)

    assert all(
        item["code"] != "ACCOUNT_DETAILS_REQUIRED"
        for item in repository.list_records("user", "data_quality_notices")
    )
    assert repository.get_record("user", "candidates", candidate_id)["status"] == "UNKNOWN"


def test_candidate_yes_promotes_detected_income_without_guessed_account_or_date() -> None:
    client, _ = _client()
    wide = (
        "record_type,account_id,name,account_type,balance,updated_at,"
        "transaction_id,occurred_at,direction,amount,description,counterparty_name\n"
        "ACCOUNT,a,main,CHECKING,1000000,2026-07-24T09:00:00+09:00,,,,,,\n"
        "TRANSACTION,a,,,,,t1,2026-01-01T09:00:00+09:00,INFLOW,1000000,project,client\n"
        "TRANSACTION,a,,,,,t2,2026-02-01T09:00:00+09:00,INFLOW,1000000,project,client\n"
        "TRANSACTION,a,,,,,t3,2026-03-01T09:00:00+09:00,INFLOW,1000000,project,client\n"
    )
    with client:
        existing = client.post(
            "/api/v1/counterparties",
            json={
                "counterparty_id": "existing-client",
                "name": "Client",
                "counterparty_type": "CLIENT",
            },
        )
        assert existing.status_code == 201
        imported = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("wide.csv", wide, "text/csv")},
        )
        candidate = imported.json()["candidates"][0]
        response = client.patch(
            f"/api/v1/candidates/{candidate['candidate_id']}",
            json={"decision": "CONFIRMED"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["promotion"]["kind"] == "receivables"
        promoted = payload["promotion"]["record"]
        assert promoted["destination_account_id"] == "a"
        assert promoted["expected_date"] == "2026-04-01"
        assert promoted["counterparty_id"] == "existing-client"
        assert [
            item["counterparty_id"] for item in client.get("/api/v1/counterparties").json()
        ] == ["existing-client"]


def test_candidate_yes_promotes_fixed_expense_with_atomic_counterparty() -> None:
    client, repository = _client()
    csv_text = (
        "transaction_id,account_id,occurred_at,direction,amount,description,"
        "counterparty_name\n"
        "rent-1,a,2026-01-05T09:00:00+09:00,OUTFLOW,500000,월세,건물주\n"
        "rent-2,a,2026-02-05T09:00:00+09:00,OUTFLOW,500000,월세,건물주\n"
    )
    with client:
        _post_account(client, "a", 1_000_000, payment=True)
        unrelated = client.post(
            "/api/v1/counterparties",
            json={
                "counterparty_id": "unrelated",
                "name": "다른 거래처",
                "counterparty_type": "CLIENT",
            },
        )
        assert unrelated.status_code == 201
        imported = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("fixed.csv", csv_text, "text/csv")},
        )
        candidate = next(
            item
            for item in imported.json()["candidates"]
            if item["candidate_type"] == "FIXED_EXPENSE"
        )

        response = client.patch(
            f"/api/v1/candidates/{candidate['candidate_id']}",
            json={"decision": "CONFIRMED"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["revision"]
        assert payload["analysis_required"] is True
        promoted = payload["promotion"]["record"]
        counterparty_id = promoted["counterparty_id"]
        assert counterparty_id is not None
        assert (
            repository.get_record("demo-user", "counterparties", counterparty_id)["name"]
            == "건물주"
        )


def test_candidate_yes_promotes_installment_when_payment_date_is_confirmed() -> None:
    client, repository = _client()
    csv_text = (
        "transaction_id,account_id,occurred_at,direction,amount,description,"
        "card_id,installment_months\n"
        "purchase-1,a,2026-07-20T09:00:00+09:00,OUTFLOW,600000,카메라,"
        "card,6\n"
    )
    repository.apply_record_bundle(
        "demo-user",
        {
            "accounts": [
                {
                    "account_id": "a",
                    "name": "main",
                    "account_type": "CHECKING",
                    "balance": 1_000_000,
                    "minimum_balance": 0,
                    "protected_amount": 0,
                    "is_payment_account": True,
                    "is_available_for_transfer": True,
                    "updated_at": "2026-07-24T09:00:00+09:00",
                }
            ],
            "cards": [
                {
                    "card_id": "card",
                    "name": "사업 카드",
                    "payment_account_id": "a",
                    "payment_day": 25,
                    "current_billing_amount": 0,
                    "billing_date": "2026-08-25",
                    "updated_at": "2026-07-24T09:00:00+09:00",
                }
            ],
        },
    )
    with client:
        imported = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("installment.csv", csv_text, "text/csv")},
        )
        candidate = next(
            item
            for item in imported.json()["candidates"]
            if item["candidate_type"] == "INSTALLMENT"
        )

        response = client.patch(
            f"/api/v1/candidates/{candidate['candidate_id']}",
            json={
                "decision": "CONFIRMED",
                "details": {"next_payment_date": "2026-08-25"},
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["revision"]
        assert payload["analysis_required"] is True
        assert payload["promotion"]["kind"] == "installment_plans"
        assert payload["promotion"]["record"]["monthly_payment"] == 100_000


def test_import_rejects_out_of_range_preference_before_persisting() -> None:
    client, repository = _client()
    with client:
        response = client.post(
            "/api/v1/imports/transactions",
            files={
                "file": (
                    "wide.csv",
                    "record_type,account_id,name,account_type,balance\n"
                    "ACCOUNT,a,main,CHECKING,100\n",
                    "text/csv",
                )
            },
            data={"protection_level": "2.5"},
        )
        assert response.status_code == 422
        assert repository.list_records("demo-user", "accounts") == []
        assert repository.list_records("demo-user", "preferences") == []


def test_analysis_as_of_is_normalized_to_asia_seoul() -> None:
    client, _ = _client()
    with client:
        _post_account(client, "a", 100_000, payment=True)
        analysis = client.post(
            "/api/v1/analyses",
            json={"as_of": "2026-07-23T16:30:00+00:00"},
        )
        report = client.get("/api/v1/reports/latest").json()

        assert analysis.json()["status"] == "COMPLETED"
        assert report["as_of"].startswith("2026-07-24T01:30:00+09:00")
        assert report["cashflow"]["daily_positions"][0]["date"] == "2026-07-24"


def test_event_query_includes_normalized_sources_and_validates_filters() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    updated_at = "2026-07-24T09:00:00+09:00"
    repository.apply_record_bundle(
        "user",
        {
            "accounts": [
                {
                    "account_id": "a",
                    "name": "main",
                    "account_type": "CHECKING",
                    "balance": 1_000_000,
                    "minimum_balance": 0,
                    "protected_amount": 0,
                    "is_payment_account": True,
                    "is_available_for_transfer": True,
                    "updated_at": updated_at,
                }
            ],
            "cards": [
                {
                    "card_id": "card",
                    "name": "card",
                    "payment_account_id": "a",
                    "payment_day": 25,
                    "current_billing_amount": 100_000,
                    "billing_date": "2026-07-25",
                    "updated_at": updated_at,
                }
            ],
            "counterparties": [
                {
                    "counterparty_id": "client",
                    "name": "client",
                    "updated_at": updated_at,
                }
            ],
            "receivables": [
                {
                    "receivable_id": "r",
                    "counterparty_id": "client",
                    "amount": 200_000,
                    "expected_date": "2026-07-26",
                    "status": "ESTIMATED",
                    "destination_account_id": "a",
                    "user_confirmed": True,
                    "source": "USER_INPUT",
                    "updated_at": updated_at,
                }
            ],
            "installment_plans": [
                {
                    "installment_plan_id": "i",
                    "card_id": "card",
                    "original_amount": 120_000,
                    "monthly_payment": 40_000,
                    "total_months": 3,
                    "remaining_months": 3,
                    "next_payment_date": "2026-08-25",
                    "status": "ACTIVE",
                }
            ],
        },
    )
    snapshot = SnapshotBuilder(repository).build("user", as_of=datetime(2026, 7, 24, tzinfo=UTC))
    tools = CoreToolService(repository)
    result = tools.query_financial_events(
        snapshot.snapshot_id,
        date_from=datetime(2026, 7, 24, tzinfo=UTC).date(),
        date_to=datetime(2026, 10, 1, tzinfo=UTC).date(),
    )

    assert {item["event_type"] for item in result["events"]} >= {
        "CARD_BILL",
        "RECEIVABLE",
        "INSTALLMENT_PAYMENT",
    }
    with pytest.raises(ServiceError) as caught:
        tools.query_financial_events(
            snapshot.snapshot_id,
            date_from=datetime(2026, 7, 24, tzinfo=UTC).date(),
            date_to=datetime(2026, 7, 25, tzinfo=UTC).date(),
            directions=["SIDEWAYS"],
        )
    assert caught.value.code == "INVALID_FINANCIAL_EVENT"


def test_receivable_and_risk_read_models_include_evidence() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    updated_at = "2026-07-24T09:00:00+09:00"
    repository.apply_record_bundle(
        "demo-user",
        {
            "accounts": [
                {
                    "account_id": "a",
                    "name": "main",
                    "account_type": "CHECKING",
                    "balance": 100_000,
                    "minimum_balance": 0,
                    "protected_amount": 0,
                    "is_payment_account": True,
                    "is_available_for_transfer": True,
                    "updated_at": updated_at,
                }
            ],
            "cards": [
                {
                    "card_id": "card",
                    "name": "card",
                    "payment_account_id": "a",
                    "payment_day": 25,
                    "current_billing_amount": 150_000,
                    "billing_date": "2026-07-25",
                    "updated_at": updated_at,
                }
            ],
            "counterparties": [
                {
                    "counterparty_id": "client",
                    "name": "디자인컴퍼니",
                    "updated_at": updated_at,
                }
            ],
            "payment_histories": [
                {
                    "payment_history_id": f"h{index}",
                    "counterparty_id": "client",
                    "expected_date": f"2026-0{index}-01",
                    "actual_date": f"2026-0{index}-0{delay + 1}",
                    "amount": 200_000,
                }
                for index, delay in enumerate((0, 2, 4, 6), start=1)
            ],
            "receivables": [
                {
                    "receivable_id": "r",
                    "counterparty_id": "client",
                    "amount": 200_000,
                    "expected_date": "2026-07-30",
                    "status": "ESTIMATED",
                    "destination_account_id": "a",
                    "user_confirmed": True,
                    "source": "USER_INPUT",
                    "updated_at": updated_at,
                }
            ],
        },
    )
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        analysis = client.post(
            "/api/v1/analyses",
            json={"as_of": "2026-07-24T09:00:00+09:00"},
        )
        assert analysis.json()["status"] == "COMPLETED"

        receivable = client.get("/api/v1/receivables").json()[0]
        assert receivable["counterparty_name"] == "디자인컴퍼니"
        assert receivable["counterparty_evidence"]["payment_history_count"] == 4
        assert receivable["counterparty_evidence"]["average_delay_days"] == 3
        assert receivable["counterparty_evidence"]["data_confidence"] == 1

        risk = client.get("/api/v1/risks/next").json()
        assert risk["triggering_events"][0]["event_id"] == "card-bill:card"
        assert risk["triggering_events"][0]["amount"] == 150_000
        assert risk["causes"] == [risk["presentation"]["cause"]]
