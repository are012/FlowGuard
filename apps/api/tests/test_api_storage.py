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
    completed = repository.transition_analysis(
        run["analysis_id"],
        "COMPLETED",
        message="done",
        result={
            "report_available": True,
            "analysis_status": "SUCCEEDED",
            "interpretation_status": "NOT_REQUESTED",
        },
    )
    assert repository.promote_report_if_current(
        user_id="user-1",
        analysis_id=run["analysis_id"],
        snapshot_revision=repository.current_state_revision("user-1"),
    )

    assert repository.latest_report("user-1")["status"] == "STABLE"
    assert completed["analysis_status"] == "SUCCEEDED"
    assert completed["interpretation_status"] == "NOT_REQUESTED"
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


def test_data_revision_is_monotonic_even_when_payload_returns_to_old_value(
    repository: FlowGuardRepository,
) -> None:
    repository.upsert_records(
        "user-1",
        "accounts",
        [{"account_id": "account-1", "balance": 100}],
    )
    first = repository.current_state_revision("user-1")
    repository.patch_record("user-1", "accounts", "account-1", {"balance": 200})
    second = repository.current_state_revision("user-1")
    repository.patch_record("user-1", "accounts", "account-1", {"balance": 100})
    third = repository.current_state_revision("user-1")

    assert (first, second, third) == ("rev-1", "rev-2", "rev-3")


def test_data_revision_is_not_reused_after_reset(repository: FlowGuardRepository) -> None:
    repository.upsert_records(
        "user-1",
        "accounts",
        [{"account_id": "account-1", "balance": 100}],
    )
    before_reset = repository.current_state_revision("user-1")
    repository.reset_user_data("user-1")
    after_reset = repository.current_state_revision("user-1")
    repository.upsert_records(
        "user-1",
        "accounts",
        [{"account_id": "account-1", "balance": 100}],
    )
    after_reimport = repository.current_state_revision("user-1")

    assert (before_reset, after_reset, after_reimport) == ("rev-1", "rev-2", "rev-3")


def test_interpretation_run_is_idempotent_and_persists_audit_fields(
    repository: FlowGuardRepository,
) -> None:
    snapshot = repository.create_snapshot(
        "user-1",
        as_of=datetime(2026, 1, 2, tzinfo=UTC),
    )
    analysis = repository.create_analysis("user-1", trigger_type="MANUAL")
    repository.transition_analysis(
        analysis["analysis_id"],
        "SNAPSHOT_BUILDING",
        message="snapshot",
        snapshot_id=snapshot["snapshot_id"],
    )
    values = {
        "analysis_id": analysis["analysis_id"],
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_revision": "rev-0",
        "request_id": "ai-request-1",
        "idempotency_key": "analysis:rev-0:contract-1.1:prompt-3:ko-KR",
        "contract_version": "1.1",
        "prompt_version": "3",
        "model_name": "test-model",
    }

    first, created = repository.create_or_get_interpretation_run(**values)
    second, created_again = repository.create_or_get_interpretation_run(**values)
    completed = repository.update_interpretation_run(
        first["interpretation_id"],
        status="FALLBACK",
        attempt_count=2,
        fallback_used=True,
        latency_ms=25,
        response_payload={"riskExplanation": "fallback"},
        error_code="http_503",
    )

    assert created is True
    assert created_again is False
    assert second["interpretation_id"] == first["interpretation_id"]
    assert completed["attempt_count"] == 2
    assert completed["fallback_used"] is True
    assert completed["error_code"] == "http_503"

    with pytest.raises(StorageConflict):
        repository.create_or_get_interpretation_run(**{**values, "request_id": "different-request"})


def test_stale_report_cannot_replace_latest_pointer(repository: FlowGuardRepository) -> None:
    repository.upsert_records(
        "user-1",
        "accounts",
        [{"account_id": "account-1", "balance": 100}],
    )
    revision_1 = repository.current_state_revision("user-1")
    snapshot_1 = repository.create_snapshot("user-1", as_of=datetime(2026, 1, 2, tzinfo=UTC))
    run_1 = _complete_minimal_report(repository, "user-1", snapshot_1, revision_1)
    assert repository.promote_report_if_current(
        user_id="user-1",
        analysis_id=run_1["analysis_id"],
        snapshot_revision=revision_1,
    )

    repository.patch_record("user-1", "accounts", "account-1", {"balance": 200})
    revision_2 = repository.current_state_revision("user-1")
    snapshot_2 = repository.create_snapshot("user-1", as_of=datetime(2026, 1, 3, tzinfo=UTC))
    run_2 = _complete_minimal_report(repository, "user-1", snapshot_2, revision_2)
    assert repository.promote_report_if_current(
        user_id="user-1",
        analysis_id=run_2["analysis_id"],
        snapshot_revision=revision_2,
    )

    assert not repository.promote_report_if_current(
        user_id="user-1",
        analysis_id=run_1["analysis_id"],
        snapshot_revision=revision_1,
    )
    assert repository.latest_report("user-1")["analysis_id"] == run_2["analysis_id"]


def _complete_minimal_report(
    repository: FlowGuardRepository,
    user_id: str,
    snapshot: dict[str, object],
    revision: str,
) -> dict[str, object]:
    run = repository.create_analysis(user_id, trigger_type="MANUAL")
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
        user_id=user_id,
        snapshot_id=str(snapshot["snapshot_id"]),
        payload={"analysis_id": run["analysis_id"], "current_state_revision": revision},
    )
    return repository.transition_analysis(
        run["analysis_id"],
        "COMPLETED",
        message="done",
        result={"analysis_status": "SUCCEEDED"},
    )


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
