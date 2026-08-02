from __future__ import annotations

from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.services.tools import CoreToolService
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
            "counterparties": 3,
            "payment_histories": 14,
            "scheduled_events": 2,
            "receivables": 3,
            "installment_plans": 1,
            "protected_funds": 1,
            "candidates": 5,
        }

        analysis = client.post(
            "/api/v1/analyses",
            headers=headers,
            json={
                "trigger_type": "DATA_REFRESH",
                "as_of": "2026-07-31T09:00:00+09:00",
            },
        )
        assert analysis.status_code == 201
        assert analysis.json()["status"] == "COMPLETED"

        dashboard = client.get("/api/v1/dashboard", headers=headers)
        assert dashboard.status_code == 200
        dashboard_payload = dashboard.json()
        assert dashboard_payload["safe_to_spend"]["safe_to_spend"] == 350_000
        assert dashboard_payload["risk_metrics"]["shortfall_type"] == "PAYMENT_ACCOUNT"
        assert dashboard_payload["risk_metrics"]["expected_gap_min"] == 50_000
        assert dashboard_payload["risk_metrics"]["expected_gap_max"] == 250_000
        assert dashboard_payload["presentation"]["impact"] == (
            "약 50,000~250,000원이 부족할 수 있습니다"
        )
        assert dashboard_payload["risk_metrics"]["data_confidence"] == 0.5
        assert dashboard_payload["presentation"]["confidence_label"] == (
            "예정 수입을 확인할수록 분석이 정밀해져요"
        )
        assert dashboard_payload["presentation"]["status"] == "PREPARE"
        primary_evidence = CoreToolService(repository).get_counterparty_evidence(
            dashboard_payload["snapshot_id"], "client-a"
        )
        assert primary_evidence["payment_history_count"] == 6
        assert primary_evidence["data_confidence"] == 1.0
        assert dashboard_payload["recommendation"]["actions"][0]["type"] == "transfer"
        comparison = dashboard_payload["recommendation"]["comparison_candidates"]
        assert len(comparison) >= 2
        assert any(item["selected"] for item in comparison)
        assert any(not item["feasible"] for item in comparison)
        tool_calls = client.get("/api/v1/reports/latest", headers=headers).json()["agent"][
            "tool_calls"
        ]
        assert len(tool_calls) <= 10
        assert [item["sequence"] for item in tool_calls] == list(range(1, len(tool_calls) + 1))

        timeline = client.get("/api/v1/cashflow/timeline", headers=headers).json()
        valid_statuses = {"STABLE", "VERIFY", "PREPARE", "ACT_NOW"}
        assert {position["status"] for position in timeline["daily_positions"]} <= valid_statuses
        status_counts = Counter(position["status"] for position in timeline["daily_positions"])
        assert status_counts == {"STABLE": 76, "VERIFY": 13, "ACT_NOW": 2}
        assert [
            position["date"]
            for position in timeline["daily_positions"]
            if position["status"] == "ACT_NOW"
        ] == ["2026-08-25", "2026-08-26"]
        assert timeline["balance_basis"] == "PAYMENT_ACCOUNT"
        assert timeline["balance_basis_label"] == "결제계좌 안전여유"
        first_four_weeks = timeline["daily_positions"][:28]
        assert (
            min(position["worst_case_safety_margin"] for position in first_four_weeks) == -250_000
        )
        assert len({position["worst_case_safety_margin"] for position in first_four_weeks}) > 1
        assert len(timeline["weekly_positions"]) == 13
        assert all(
            weekly["causes"]
            for weekly in timeline["weekly_positions"]
            if weekly["status"] in {"PREPARE", "ACT_NOW"}
        )
        risk_position = next(
            position
            for position in timeline["daily_positions"]
            if position["date"] == dashboard_payload["risk_metrics"]["first_risk_date"]
        )
        assert risk_position["status"] == "ACT_NOW"
        weekly_positions = timeline["weekly_positions"]
        assert [position["week"] for position in weekly_positions] == list(range(1, 14))
        risk_week = next(
            position
            for position in weekly_positions
            if position["start_date"]
            <= dashboard_payload["risk_metrics"]["first_risk_date"]
            <= position["end_date"]
        )
        assert risk_week["status"] == "ACT_NOW"
        assert len(risk_week["causes"]) == len(set(risk_week["causes"]))
        assert any("생활비 카드 결제대금" in cause for cause in risk_week["causes"][:2])
        assert any("사업용 카드 할부금" in cause for cause in risk_week["causes"][:2])
        risk_week_index = risk_week["week"] - 1
        scenario_week = [
            position
            for scenario in timeline["scenarios"]
            for position in scenario["daily_positions"][
                risk_week_index * 7 : (risk_week_index + 1) * 7
            ]
        ]
        assert risk_week["min_available_balance"] == min(
            position["available_balance"] for position in scenario_week
        )
        margin_field = (
            "payment_account_margin"
            if dashboard_payload["risk_metrics"]["shortfall_type"] == "PAYMENT_ACCOUNT"
            else "liquidity_margin"
        )
        scenario_margins = []
        for position in scenario_week:
            if margin_field in position:
                scenario_margins.append(position[margin_field])
        if scenario_margins:
            assert risk_week["min_safety_margin"] == min(scenario_margins)
        else:
            assert risk_week["min_safety_margin"] is None

        receivables = {
            item["receivable_id"]: item
            for item in client.get("/api/v1/receivables", headers=headers).json()
        }
        assert set(receivables) == {
            "receivable-a-next",
            "receivable-b-next",
            "receivable-c-next",
        }
        assert receivables["receivable-a-next"]["counterparty_name"] == "디자인컴퍼니A"
        assert receivables["receivable-a-next"]["status"] == "CONFIRMED"
        assert receivables["receivable-b-next"]["counterparty_name"] == "콘텐츠랩B"
        delayed_evidence = receivables["receivable-b-next"]["counterparty_evidence"]
        assert delayed_evidence["payment_history_count"] == 4
        assert delayed_evidence["average_delay_days"] == 5.75
        assert delayed_evidence["recent_trend"] == "WORSENING"
        assert receivables["receivable-c-next"]["counterparty_name"] == "교육스튜디오C"
        steady_evidence = receivables["receivable-c-next"]["counterparty_evidence"]
        assert steady_evidence["payment_history_count"] == 4
        assert steady_evidence["average_delay_days"] == 1.5
        assert steady_evidence["maximum_delay_days"] == 3

        risk = client.get("/api/v1/risks/next", headers=headers).json()
        assert {event["event_type"] for event in risk["triggering_events"]} == {
            "CARD_BILL",
            "INSTALLMENT_PAYMENT",
        }
        assert all(event["description"] for event in risk["triggering_events"])
        assert any(
            "생활비 카드 결제대금" in event["description"] for event in risk["triggering_events"]
        )
        assert any(
            "사업용 카드 할부금" in event["description"] for event in risk["triggering_events"]
        )
        assert risk["causes"] == [risk["presentation"]["cause"]]

        recommendations = client.get("/api/v1/recommendations", headers=headers).json()
        assert len(recommendations) == 1
        recommendation = recommendations[0]
        assert recommendation["title"] == "결제계좌에 250,000원을 미리 옮기세요"
        assert recommendation["summary"]
        assert recommendation["rationale"]
        assert "baseline_result" not in recommendation["rationale"]
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
