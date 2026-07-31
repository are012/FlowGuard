from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.storage import FlowGuardRepository

SAMPLE_CSV = (
    Path(__file__).parents[2]
    / "web"
    / "public"
    / "samples"
    / "flowguard-synthetic-transactions.csv"
)


def test_sample_csv_runs_the_complete_virtual_recommendation_flow() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    headers = {"X-User-ID": "sample-flow"}

    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        with SAMPLE_CSV.open("rb") as sample:
            imported = client.post(
                "/api/v1/imports/transactions",
                headers=headers,
                files={"file": (SAMPLE_CSV.name, sample, "text/csv")},
                data={"income_type": "MIXED", "minimum_total_reserve": "0"},
            )

        assert imported.status_code == 201
        import_payload = imported.json()
        assert import_payload["format"] == "WIDE_RECORDS"
        assert import_payload["revision"]
        assert import_payload["analysis_required"] is True
        assert import_payload["imported_counts"] == {
            "accounts": 3,
            "cards": 2,
            "transactions": 23,
            "counterparties": 2,
            "payment_histories": 4,
            "scheduled_events": 2,
            "receivables": 1,
            "installment_plans": 1,
            "protected_funds": 1,
            "candidates": 5,
        }

        analysis = client.post(
            "/api/v1/analyses",
            headers=headers,
            json={
                "trigger_type": "DATA_REFRESH",
                "as_of": "2026-07-24T09:00:00+09:00",
            },
        )
        assert analysis.status_code == 201
        assert analysis.json()["status"] == "COMPLETED"

        analysis = client.post(
            "/api/v1/analyses",
            headers=headers,
            json={
                "trigger_type": "DATA_REFRESH",
                "as_of": "2026-07-24T09:00:00+09:00",
            },
        )
        assert analysis.status_code == 201
        assert analysis.json()["status"] == "COMPLETED"

        dashboard = client.get("/api/v1/dashboard", headers=headers)
        assert dashboard.status_code == 200
        dashboard_payload = dashboard.json()
        assert dashboard_payload["safe_to_spend"]["safe_to_spend"] == 100_000
        assert dashboard_payload["risk_metrics"]["shortfall_type"] == "PAYMENT_ACCOUNT"
        assert dashboard_payload["risk_metrics"]["expected_gap_max"] == 250_000
        assert dashboard_payload["recommendation"]["actions"][0]["type"] == "transfer"

        receivables = client.get("/api/v1/receivables", headers=headers).json()
        assert receivables[0]["counterparty_name"] == "콘텐츠랩B"
        evidence = receivables[0]["counterparty_evidence"]
        assert evidence["payment_history_count"] == 4
        assert evidence["average_delay_days"] == 5.75
        assert evidence["recent_trend"] == "WORSENING"

        risk = client.get("/api/v1/risks/next", headers=headers).json()
        assert {event["event_type"] for event in risk["triggering_events"]} == {
            "CARD_BILL",
            "INSTALLMENT_PAYMENT",
        }
        assert risk["causes"] == [risk["presentation"]["cause"]]

        recommendations = client.get("/api/v1/recommendations", headers=headers).json()
        assert len(recommendations) == 1
        recommendation = recommendations[0]
        approved = client.post(
            (f"/api/v1/recommendations/{recommendation['recommendation_id']}/approve"),
            headers=headers,
            json={},
        )

        assert approved.status_code == 200
        approval_payload = approved.json()
        assert approval_payload["virtual_analysis"]["status"] == "COMPLETED"
        assert approval_payload["virtual_report"]["is_virtual"] is True
        assert approval_payload["virtual_application"]["external_actions_executed"] is False
        assert {
            account["account_id"]: account["balance"]
            for account in client.get("/api/v1/accounts", headers=headers).json()
        } == {
            "account-business": 1_200_000,
            "account-main": 800_000,
            "account-tax": 1_000_000,
        }

        refreshed_dashboard = client.get("/api/v1/dashboard", headers=headers).json()
        assert refreshed_dashboard["is_virtual"] is True
        assert refreshed_dashboard["recommendation"] is None


def test_every_sample_candidate_can_be_confirmed_and_reanalyzed() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    headers = {"X-User-ID": "sample-confirmations"}

    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        with SAMPLE_CSV.open("rb") as sample:
            imported = client.post(
                "/api/v1/imports/transactions",
                headers=headers,
                files={"file": (SAMPLE_CSV.name, sample, "text/csv")},
            ).json()

        confirmed_types: list[str] = []
        for candidate in imported["candidates"]:
            details = (
                {"next_payment_date": "2026-08-25"}
                if candidate["candidate_type"] == "INSTALLMENT"
                else None
            )
            response = client.patch(
                f"/api/v1/candidates/{candidate['candidate_id']}",
                headers=headers,
                json={"decision": "CONFIRMED", "details": details},
            )

            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "CONFIRMED"
            assert payload["revision"]
            assert payload["analysis_required"] is True
            confirmed_types.append(candidate["candidate_type"])

        assert confirmed_types.count("RECURRING_INCOME") == 2
        assert confirmed_types.count("FIXED_EXPENSE") == 2
        assert confirmed_types.count("INSTALLMENT") == 1
