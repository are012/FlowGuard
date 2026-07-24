from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flowguard.services.data import DataService
from flowguard.storage import (
    FlowGuardRepository,
    InvalidAnalysisTransition,
    StorageConflict,
)


@pytest.fixture
def repository() -> FlowGuardRepository:
    return FlowGuardRepository("sqlite:///:memory:")


def test_repository_persists_current_state_but_snapshots_are_immutable(
    repository: FlowGuardRepository,
) -> None:
    repository.upsert_records(
        "user-1",
        "accounts",
        [
            {
                "account_id": "account-1",
                "name": "생활비",
                "account_type": "CHECKING",
                "balance": 800_000,
                "minimum_balance": 100_000,
                "protected_amount": 0,
                "is_payment_account": True,
                "is_available_for_transfer": True,
                "updated_at": "2026-01-01T00:00:00+09:00",
            }
        ],
    )

    first = repository.create_snapshot(
        "user-1",
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
        snapshot_id="snapshot-fixed",
    )
    repository.patch_record("user-1", "accounts", "account-1", {"balance": 700_000})

    assert first["accounts"][0]["balance"] == 800_000
    assert repository.get_snapshot("snapshot-fixed")["accounts"][0]["balance"] == 800_000
    with pytest.raises(StorageConflict):
        repository.create_snapshot(
            "user-1",
            as_of=datetime(2026, 1, 3, tzinfo=UTC),
            snapshot_id="snapshot-fixed",
        )


def test_analysis_workflow_records_ordered_events_and_latest_completed_report(
    repository: FlowGuardRepository,
) -> None:
    snapshot = repository.create_snapshot(
        "user-1",
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
    )
    run = repository.create_analysis("user-1", trigger_type="MANUAL")

    for status in (
        "SNAPSHOT_BUILDING",
        "BASELINE_ANALYZING",
        "AGENT_INVESTIGATING",
        "PLAN_EVALUATING",
        "REPORT_BUILDING",
    ):
        run = repository.transition_analysis(
            run["analysis_id"],
            status,
            message=status,
            snapshot_id=snapshot["snapshot_id"] if status == "SNAPSHOT_BUILDING" else None,
        )

    repository.save_report(
        analysis_id=run["analysis_id"],
        user_id="user-1",
        snapshot_id=snapshot["snapshot_id"],
        payload={"analysis_id": run["analysis_id"], "status": "STABLE"},
    )
    repository.transition_analysis(
        run["analysis_id"],
        "COMPLETED",
        message="done",
        result={"report_available": True},
    )

    assert repository.latest_report("user-1")["status"] == "STABLE"
    events = repository.list_analysis_events(run["analysis_id"])
    assert [event["status"] for event in events] == [
        "QUEUED",
        "SNAPSHOT_BUILDING",
        "BASELINE_ANALYZING",
        "AGENT_INVESTIGATING",
        "PLAN_EVALUATING",
        "REPORT_BUILDING",
        "COMPLETED",
    ]
    with pytest.raises(InvalidAnalysisTransition):
        repository.transition_analysis(run["analysis_id"], "FAILED", message="too late")


def test_recommendation_decision_is_audited_as_virtual_only(
    repository: FlowGuardRepository,
) -> None:
    snapshot = repository.create_snapshot(
        "user-1",
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
    )
    run = repository.create_analysis("user-1", trigger_type="MANUAL")
    run = repository.transition_analysis(
        run["analysis_id"],
        "SNAPSHOT_BUILDING",
        message="snapshot",
        snapshot_id=snapshot["snapshot_id"],
    )
    recommendation = repository.save_recommendation(
        analysis_id=run["analysis_id"],
        user_id="user-1",
        recommendation_id="recommendation-1",
        payload={"actions": []},
    )

    approved = repository.record_recommendation_decision(
        "user-1",
        recommendation["recommendation_id"],
        decision="APPROVED",
        details={"confirmed_virtual_only": True},
    )

    assert approved["status"] == "APPROVED"
    audit = repository.list_approval_audit("user-1", "recommendation-1")
    assert audit[0]["effect"] == "VIRTUAL_ONLY"


def test_data_service_imports_candidates_without_confirming_them(
    repository: FlowGuardRepository,
) -> None:
    csv_text = """transaction_id,account_id,occurred_at,direction,amount,description
t1,a,2026-01-01T09:00:00+09:00,OUTFLOW,500000,월세
t2,a,2026-02-01T09:00:00+09:00,OUTFLOW,500000,월세
"""

    imported = DataService(repository).import_csv("user-1", csv_text)

    assert imported["candidates"][0]["status"] == "PENDING"
    stored = repository.list_records("user-1", "candidates")
    assert stored[0]["status"] == "PENDING"
    assert repository.list_records("user-1", "data_quality_notices")
