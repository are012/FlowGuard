from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flowguard.services.data import DataService
from flowguard.storage import (
    FlowGuardRepository,
    InvalidAnalysisTransition,
    RecordNotFound,
    StorageConflict,
    StorageError,
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


def test_classification_run_is_idempotent_and_persists_only_audit_fields(
    repository: FlowGuardRepository,
) -> None:
    values = {
        "user_id": "user-1",
        "import_id": "import-1",
        "request_id": "ai-classify-1",
        "idempotency_key": "import-1:labels-hash:contract-1.2:classify-1:ko-KR",
        "label_set_hash": "labels-hash",
        "schema_version": "1.2",
        "contract_version": "1.2",
        "prompt_version": "classify-1",
        "mode": "ON",
        "label_count": 4,
        "model_name": "test-model",
    }

    first, created = repository.create_or_get_classification_run(**values)
    second, created_again = repository.create_or_get_classification_run(**values)
    response_payload = {"groups": [{"groupId": "G1", "labelIds": ["L1", "L2"]}]}
    repository.apply_record_bundle(
        "user-1",
        {"accounts": [{"account_id": "account-1", "balance": 100}]},
        classification_completion={
            "classification_id": first["classification_id"],
            "import_id": values["import_id"],
            "label_set_hash": values["label_set_hash"],
            "status": "SUCCEEDED",
            "applied": True,
            "attempt_count": 1,
            "fallback_used": False,
            "latency_ms": 25,
            "group_count": 2,
            "merged_label_count": 2,
            "response_payload": response_payload,
            "error_code": None,
        },
    )
    completed = repository.get_classification_run(first["classification_id"])

    assert created is True
    assert created_again is False
    assert second["classification_id"] == first["classification_id"]
    assert completed["status"] == "SUCCEEDED"
    assert completed["applied"] is True
    assert completed["attempt_count"] == 1
    assert completed["group_count"] == 2
    assert completed["merged_label_count"] == 2
    assert completed["completed_at"] is not None
    assert "labels" not in completed
    assert repository.get_record("user-1", "accounts", "account-1")["balance"] == 100
    stored = repository.get_classification_run(first["classification_id"])
    latest = repository.latest_classification_run("user-1", import_id="import-1")
    assert stored["classification_id"] == completed["classification_id"]
    assert stored["response_payload"] == completed["response_payload"]
    assert latest is not None
    assert latest["classification_id"] == completed["classification_id"]

    with pytest.raises(StorageConflict):
        repository.create_or_get_classification_run(**{**values, "request_id": "different-request"})


def test_classification_run_enforces_mode_status_and_payload_boundaries(
    repository: FlowGuardRepository,
) -> None:
    values = {
        "user_id": "user-1",
        "import_id": "import-1",
        "request_id": "ai-classify-1",
        "idempotency_key": "classification-key",
        "label_set_hash": "labels-hash",
        "schema_version": "1.2",
        "contract_version": "1.2",
        "prompt_version": "classify-1",
        "mode": "SHADOW",
        "label_count": 2,
    }
    run, _created = repository.create_or_get_classification_run(**values)

    with pytest.raises(StorageError):
        repository.update_classification_run(
            run["classification_id"], status="SUCCEEDED", applied=True
        )
    with pytest.raises(StorageError):
        repository.update_classification_run(
            run["classification_id"],
            status="REJECTED",
            response_payload={"untrusted": "response"},
        )
    with pytest.raises(StorageError):
        repository.update_classification_run(run["classification_id"], status="UNKNOWN")
    with pytest.raises(StorageError):
        repository.create_or_get_classification_run(**{**values, "mode": "OFF"})


def test_classification_terminal_state_is_immutable_and_identical_update_is_safe(
    repository: FlowGuardRepository,
) -> None:
    values = {
        "user_id": "user-1",
        "import_id": "import-1",
        "request_id": "ai-classify-1",
        "idempotency_key": "classification-terminal-key",
        "label_set_hash": "labels-hash",
        "schema_version": "1.2",
        "contract_version": "1.2",
        "prompt_version": "classify-1",
        "mode": "ON",
        "label_count": 2,
    }
    run, _ = repository.create_or_get_classification_run(**values)
    failed = repository.update_classification_run(
        run["classification_id"],
        status="FAILED",
        applied=False,
        attempt_count=2,
        fallback_used=True,
        latency_ms=10,
        error_code="connection_failed",
    )
    repeated = repository.update_classification_run(
        run["classification_id"],
        status="FAILED",
        applied=False,
        attempt_count=2,
        fallback_used=True,
        latency_ms=10,
        error_code="connection_failed",
    )

    assert repeated["classification_id"] == failed["classification_id"]
    assert repeated["status"] == failed["status"]
    assert repeated["error_code"] == failed["error_code"]
    with pytest.raises(StorageConflict):
        repository.update_classification_run(
            run["classification_id"],
            status="SUCCEEDED",
            applied=True,
            group_count=1,
            merged_label_count=1,
            response_payload={"groups": [], "ungrouped": ["L1", "L2"]},
        )


def test_sqlite_applied_classification_cannot_be_overwritten_by_failed_terminal(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_classification_run(
        user_id="user-1",
        import_id="import-success-wins",
        request_id="ai-classify-success-wins",
        idempotency_key="classification-success-wins",
        label_set_hash="labels-hash",
        schema_version="1.2",
        contract_version="1.2",
        prompt_version="classify-1",
        mode="ON",
        label_count=2,
    )
    repository.apply_record_bundle(
        "user-1",
        {"accounts": [{"account_id": "account-success", "balance": 100}]},
        classification_completion={
            "classification_id": run["classification_id"],
            "import_id": "import-success-wins",
            "label_set_hash": "labels-hash",
            "status": "SUCCEEDED",
            "applied": True,
            "attempt_count": 1,
            "fallback_used": False,
            "latency_ms": 5,
            "group_count": 1,
            "merged_label_count": 1,
            "response_payload": {"groups": [], "ungrouped": ["L1", "L2"]},
            "error_code": None,
        },
    )

    with pytest.raises(StorageConflict):
        repository.update_classification_run(
            run["classification_id"],
            status="FAILED",
            applied=False,
            attempt_count=1,
            fallback_used=True,
            latency_ms=7,
            error_code="late_failure",
        )

    stored = repository.get_classification_run(run["classification_id"])
    assert stored["status"] == "SUCCEEDED"
    assert stored["applied"] is True
    assert repository.get_record("user-1", "accounts", "account-success")["balance"] == 100


def test_sqlite_failed_classification_blocks_atomic_success_and_rolls_back_bundle(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_classification_run(
        user_id="user-1",
        import_id="import-failure-wins",
        request_id="ai-classify-failure-wins",
        idempotency_key="classification-failure-wins",
        label_set_hash="labels-hash",
        schema_version="1.2",
        contract_version="1.2",
        prompt_version="classify-1",
        mode="ON",
        label_count=2,
    )
    repository.update_classification_run(
        run["classification_id"],
        status="FAILED",
        applied=False,
        attempt_count=1,
        fallback_used=True,
        latency_ms=5,
        error_code="winner_failed",
    )
    before_revision = repository.current_state_revision("user-1")

    with pytest.raises(StorageConflict):
        repository.apply_record_bundle(
            "user-1",
            {"accounts": [{"account_id": "account-loser", "balance": 100}]},
            classification_completion={
                "classification_id": run["classification_id"],
                "import_id": "import-failure-wins",
                "label_set_hash": "labels-hash",
                "status": "SUCCEEDED",
                "applied": True,
                "attempt_count": 1,
                "fallback_used": False,
                "latency_ms": 7,
                "group_count": 1,
                "merged_label_count": 1,
                "response_payload": {"groups": [], "ungrouped": ["L1", "L2"]},
                "error_code": None,
            },
        )

    stored = repository.get_classification_run(run["classification_id"])
    assert stored["status"] == "FAILED"
    assert stored["applied"] is False
    assert repository.current_state_revision("user-1") == before_revision
    assert repository.list_records("user-1", "accounts") == []


def test_sqlite_non_applied_guard_rolls_back_when_reset_deleted_audit(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_classification_run(
        user_id="user-1",
        import_id="import-reset-guard",
        request_id="ai-classify-reset-guard",
        idempotency_key="classification-reset-guard",
        label_set_hash="labels-hash",
        schema_version="1.2",
        contract_version="1.2",
        prompt_version="classify-1",
        mode="ON",
        label_count=2,
    )
    failed = repository.update_classification_run(
        run["classification_id"],
        status="FAILED",
        applied=False,
        attempt_count=1,
        fallback_used=True,
        latency_ms=5,
        error_code="classification_failed",
    )
    repository.reset_user_data("user-1")
    before_revision = repository.current_state_revision("user-1")

    with pytest.raises(RecordNotFound):
        repository.apply_record_bundle(
            "user-1",
            {"accounts": [{"account_id": "account-after-reset", "balance": 100}]},
            classification_completion={
                "classification_id": failed["classification_id"],
                "import_id": failed["import_id"],
                "label_set_hash": failed["label_set_hash"],
                "status": "FAILED",
                "applied": False,
            },
        )

    assert repository.current_state_revision("user-1") == before_revision
    assert repository.list_records("user-1", "accounts") == []


def test_classification_success_requires_complete_validated_result(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_classification_run(
        user_id="user-1",
        import_id="import-1",
        request_id="ai-classify-1",
        idempotency_key="classification-incomplete-success",
        label_set_hash="labels-hash",
        schema_version="1.2",
        contract_version="1.2",
        prompt_version="classify-1",
        mode="ON",
        label_count=2,
    )

    with pytest.raises(StorageError):
        repository.update_classification_run(
            run["classification_id"],
            status="SUCCEEDED",
            applied=True,
        )
    with pytest.raises(StorageError):
        repository.update_classification_run(
            run["classification_id"],
            status="SUCCEEDED",
            applied=True,
            group_count=2,
            merged_label_count=2,
            response_payload={"groups": []},
        )
    with pytest.raises(StorageError, match="apply_record_bundle"):
        repository.update_classification_run(
            run["classification_id"],
            status="SUCCEEDED",
            applied=True,
            attempt_count=1,
            fallback_used=False,
            latency_ms=5,
            group_count=1,
            merged_label_count=1,
            response_payload={"groups": [], "ungrouped": ["L1", "L2"]},
        )
    assert repository.get_classification_run(run["classification_id"])["status"] == ("IN_PROGRESS")


def test_classification_completion_rejects_cross_user_and_rolls_back_bundle(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_classification_run(
        user_id="user-2",
        import_id="import-user-2",
        request_id="ai-classify-user-2",
        idempotency_key="classification-user-2-completion",
        label_set_hash="labels-hash",
        schema_version="1.2",
        contract_version="1.2",
        prompt_version="classify-1",
        mode="ON",
        label_count=2,
    )
    before_revision = repository.current_state_revision("user-1")

    with pytest.raises(StorageConflict):
        repository.apply_record_bundle(
            "user-1",
            {"accounts": [{"account_id": "account-1", "balance": 100}]},
            classification_completion={
                "classification_id": run["classification_id"],
                "import_id": "import-user-2",
                "label_set_hash": "labels-hash",
                "status": "SUCCEEDED",
                "applied": True,
                "attempt_count": 1,
                "fallback_used": False,
                "latency_ms": 5,
                "group_count": 1,
                "merged_label_count": 1,
                "response_payload": {"groups": [], "ungrouped": ["L1", "L2"]},
                "error_code": None,
            },
        )

    assert repository.current_state_revision("user-1") == before_revision
    assert repository.list_records("user-1", "accounts") == []
    assert repository.get_classification_run(run["classification_id"])["status"] == ("IN_PROGRESS")


def test_stale_ai_candidate_decision_cannot_overwrite_a_user_split(
    repository: FlowGuardRepository,
) -> None:
    source = {
        "candidate_id": "candidate-ai",
        "candidate_type": "FIXED_EXPENSE",
        "status": "PENDING",
        "classification_group": {
            "source": "AI",
            "labels": ["상호A", "상호 A"],
            "can_split": True,
        },
    }
    repository.upsert_records("user-1", "candidates", [source])
    repository.split_candidate_group(
        "user-1",
        "candidate-ai",
        alternatives=[],
        notices=[],
    )

    with pytest.raises(StorageConflict):
        repository.apply_record_bundle(
            "user-1",
            {
                "candidates": [{**source, "status": "CONFIRMED"}],
                "scheduled_events": [
                    {
                        "event_id": "event-stale",
                        "amount": 100,
                    }
                ],
            },
            candidate_preconditions={"candidate-ai": source},
        )

    assert repository.list_records("user-1", "scheduled_events") == []
    stored = repository.get_record("user-1", "candidates", "candidate-ai")
    assert stored["classification_group"]["user_split"] is True


def test_reset_user_data_deletes_only_that_users_classification_audit(
    repository: FlowGuardRepository,
) -> None:
    common = {
        "request_id": "ai-classify-1",
        "label_set_hash": "labels-hash",
        "schema_version": "1.2",
        "contract_version": "1.2",
        "prompt_version": "classify-1",
        "mode": "ON",
        "label_count": 2,
    }
    first, _ = repository.create_or_get_classification_run(
        **common,
        user_id="user-1",
        import_id="import-1",
        idempotency_key="classification-user-1",
    )
    second, _ = repository.create_or_get_classification_run(
        **common,
        user_id="user-2",
        import_id="import-2",
        idempotency_key="classification-user-2",
    )

    repository.reset_user_data("user-1")

    with pytest.raises(RecordNotFound):
        repository.get_classification_run(first["classification_id"])
    assert repository.get_classification_run(second["classification_id"])["user_id"] == "user-2"


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
