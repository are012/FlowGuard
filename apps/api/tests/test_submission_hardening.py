from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.services.investigator import LiquidityInvestigator, RiskEvidenceBuilder
from flowguard.storage import FlowGuardRepository, RecordNotFound

AS_OF = "2026-07-24T09:00:00+09:00"
DEMO_AS_OF = "2026-07-31T09:00:00+09:00"
SAMPLE_CSV = (
    Path(__file__).parents[2]
    / "web"
    / "public"
    / "samples"
    / "flowguard-synthetic-transactions.csv"
)


def _account(
    client: TestClient,
    account_id: str,
    balance: int,
    *,
    user_id: str = "demo-user",
    payment: bool = False,
) -> None:
    response = client.post(
        "/api/v1/accounts",
        headers={"X-User-ID": user_id},
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


def _payment_risk(client: TestClient) -> dict[str, Any]:
    _account(client, "payment", 50_000, payment=True)
    _account(client, "reserve", 200_000)
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
        json={"trigger_type": "DATA_REFRESH", "as_of": AS_OF},
    )
    assert analysis.status_code == 201
    assert analysis.json()["status"] == "COMPLETED"
    return client.get("/api/v1/recommendations").json()[0]


def test_demo_import_exposes_fixed_analysis_time_only_in_demo_mode() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        with SAMPLE_CSV.open("rb") as sample:
            demo = client.post(
                "/api/v1/imports/transactions",
                files={"file": (SAMPLE_CSV.name, sample, "text/csv")},
                data={"demo_mode": "true"},
            )
        assert demo.status_code == 201
        assert demo.json()["is_demo"] is True
        assert demo.json()["analysis_as_of"] == DEMO_AS_OF

        regular = client.post(
            "/api/v1/imports/transactions",
            files={
                "file": (
                    "regular.csv",
                    "record_type,account_id,name,account_type,balance\nACCOUNT,a,A,CHECKING,1000\n",
                    "text/csv",
                )
            },
        )
        assert regular.status_code == 201
        assert regular.json()["is_demo"] is False
        assert "analysis_as_of" not in regular.json()


def test_setup_commit_is_atomic_and_accepts_no_candidates() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    repository.upsert_records(
        "demo-user",
        "candidates",
        [
            {
                "candidate_id": "first",
                "candidate_type": "FIXED_EXPENSE",
                "status": "PENDING",
                "proposed_record": {},
            },
            {
                "candidate_id": "invalid",
                "candidate_type": "FIXED_EXPENSE",
                "status": "PENDING",
                "proposed_record": {},
            },
        ],
    )
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        failed = client.post(
            "/api/v1/setup/commit",
            json={
                "preferences": {"minimum_total_reserve": 12_345},
                "candidates": [
                    {"candidate_id": "first", "decision": "UNKNOWN"},
                    {"candidate_id": "invalid", "decision": "CONFIRMED"},
                ],
            },
        )
        assert failed.status_code == 422
        assert repository.list_records("demo-user", "preferences") == []
        assert {
            item["candidate_id"]: item["status"]
            for item in repository.list_records("demo-user", "candidates")
        } == {"first": "PENDING", "invalid": "PENDING"}

        committed = client.post(
            "/api/v1/setup/commit",
            json={
                "preferences": {"minimum_total_reserve": 12_345},
                "candidates": [],
            },
        )
        assert committed.status_code == 200
        payload = committed.json()
        assert payload["preferences"]["minimum_total_reserve"] == 12_345
        assert payload["candidates"] == []
        assert payload["revision"]
        assert payload["analysis_required"] is True


def test_demo_reset_is_user_scoped_atomic_and_idempotent() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        _account(client, "mine", 100_000)
        _account(client, "other", 200_000, user_id="other-user")
        analysis = client.post(
            "/api/v1/analyses",
            json={"as_of": AS_OF},
        )
        assert analysis.status_code == 201

        reset = client.post(
            "/api/v1/demo/reset",
            json={"confirmation": "RESET_DEMO"},
        )
        assert reset.status_code == 200
        assert reset.json()["reset"] is True
        assert reset.json()["analysis_required"] is False
        assert client.get("/api/v1/accounts").json() == []
        assert (
            client.get(
                "/api/v1/accounts",
                headers={"X-User-ID": "other-user"},
            ).json()[0]["account_id"]
            == "other"
        )
        assert repository.latest_analysis("demo-user") is None
        with pytest.raises(RecordNotFound):
            repository.latest_report("demo-user")

        repeated = client.post(
            "/api/v1/demo/reset",
            json={"confirmation": "RESET_DEMO"},
        )
        assert repeated.status_code == 200


def test_revision_contract_marks_stale_and_fresh_analysis() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        _account(client, "account", 100_000, payment=True)
        analysis = client.post(
            "/api/v1/analyses",
            json={"as_of": AS_OF},
        ).json()
        assert analysis["revision"] == analysis["analysis_revision"]
        assert analysis["analysis_required"] is False
        dashboard = client.get("/api/v1/dashboard").json()
        assert dashboard["analysis_required"] is False

        changed = client.patch(
            "/api/v1/accounts/account",
            json={"balance": 90_000},
        )
        assert changed.status_code == 200
        stale = client.get("/api/v1/dashboard").json()
        assert stale["revision"] != stale["analysis_revision"]
        assert stale["analysis_required"] is True


def test_recommendation_contains_derivation_and_comparison() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    with TestClient(create_app(repository), raise_server_exceptions=False) as client:
        recommendation = _payment_risk(client)

    derivation = recommendation["derivation"]
    assert derivation["amount"] == 50_000
    assert derivation["source_field"] == "risk_metrics.expected_gap_max"
    assert derivation["source_value"] == 50_000
    assert derivation["snapshot_id"] == recommendation["snapshot_id"]
    assert derivation["revision"] == recommendation["current_state_revision"]
    assert derivation["tool_version"] == "cashflow-2"
    assert derivation["policy_version"] == "policy-1"
    assert "bill" in derivation["evidence_ids"]

    comparisons = recommendation["comparison_candidates"]
    assert 1 <= len(comparisons) <= 3
    assert sum(item["selected"] for item in comparisons) == 1
    assert all("after_expected_gap_max" in item for item in comparisons)
    assert all("policy_violations" in item for item in comparisons)


def test_unexpected_ai_failure_does_not_fail_financial_analysis() -> None:
    class FailingAIClient:
        @staticmethod
        def interpret(_payload: dict[str, Any]) -> None:
            raise RuntimeError("AI service failed")

    repository = FlowGuardRepository("sqlite:///:memory:")
    app = create_app(repository)
    app.state.analysis_service.ai_client = FailingAIClient()
    with TestClient(app, raise_server_exceptions=False) as client:
        _payment_risk(client)

    analysis = repository.latest_analysis("demo-user")
    assert analysis is not None
    assert analysis["analysis_status"] == "SUCCEEDED"
    assert analysis["interpretation_status"] == "FAILED"
    assert repository.latest_report("demo-user")["analysis_id"] == analysis["analysis_id"]
    interpretation = repository.latest_interpretation_run(analysis["analysis_id"])
    assert interpretation is not None
    assert interpretation["status"] == "FAILED"
    assert interpretation["error_code"] == "interpretation_internal_error"
    assert [event["status"] for event in repository.list_analysis_events(analysis["analysis_id"])][
        -2:
    ] == ["INTERPRETATION_REQUESTING", "COMPLETED"]


def test_counterparty_evidence_is_deduplicated() -> None:
    class StubTools:
        calls = 0

        @staticmethod
        def get_financial_context(_snapshot_id: str) -> dict[str, Any]:
            return {
                "receivables": [
                    {"counterparty_id": "client"},
                    {"counterparty_id": "client"},
                ]
            }

        def get_counterparty_evidence(
            self,
            _snapshot_id: str,
            counterparty_id: str,
        ) -> dict[str, Any]:
            self.calls += 1
            return {"counterparty_id": counterparty_id}

    tools = StubTools()
    builder = RiskEvidenceBuilder(tools)  # type: ignore[arg-type]
    state = builder.build(
        "snapshot",
        {
            "risk_metrics": {
                "shortfall_type": "PAYMENT_ACCOUNT",
                "first_risk_date": "2026-07-25",
                "expected_gap_max": 10_000,
            }
        },
        lambda _name, _payload, function: function(),
    )
    assert tools.calls == 1
    assert state["gathered_evidence"] == [{"counterparty_id": "client"}]


def test_unexpected_tool_exception_is_audited() -> None:
    class FailingTools:
        @staticmethod
        def get_financial_context(_snapshot_id: str) -> dict[str, Any]:
            raise ValueError("boom")

    repository = FlowGuardRepository("sqlite:///:memory:")
    analysis = repository.create_analysis("user", trigger_type="MANUAL")
    investigator = LiquidityInvestigator(
        repository,
        FailingTools(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError):
        investigator.investigate(
            analysis_id=analysis["analysis_id"],
            snapshot_id="snapshot",
            baseline_result={"risk_metrics": {}},
        )

    audit = repository.list_tool_executions(analysis["analysis_id"])
    assert len(audit) == 1
    assert audit[0]["error"]["code"] == "TOOL_EXECUTION_FAILED"
    assert audit[0]["error"]["details"]["exception_type"] == "ValueError"
