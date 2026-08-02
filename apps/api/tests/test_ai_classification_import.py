from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowguard.main import create_app
from flowguard.services.data import DataService
from flowguard.services.errors import ServiceError
from flowguard.services.label_group_validation import LabelClassificationOutcome
from flowguard.storage import FlowGuardRepository, StorageConflict

CSV_WITH_LABEL_VARIANTS = (
    "transaction_id,account_id,occurred_at,direction,amount,description,"
    "counterparty_name\n"
    "t1,a,2026-01-01T09:00:00+09:00,OUTFLOW,500000,월세 01월,디자인(주)\n"
    "t2,a,2026-02-01T09:00:00+09:00,OUTFLOW,500000,월세 02월,디자인(주)\n"
    "t3,a,2026-03-01T09:00:00+09:00,OUTFLOW,500000,월세 03월,(주)디자인\n"
    "t4,a,2026-04-01T09:00:00+09:00,OUTFLOW,500000,월세 04월,(주)디자인\n"
)


class MustNotCallClassifier:
    def classify(self, payload: Mapping[str, Any]) -> LabelClassificationOutcome:
        raise AssertionError(f"classification must stay disabled: {payload}")


class MergingClassifier:
    def __init__(
        self,
        *,
        status: str = "SUCCEEDED",
        category_hint: str = "RENT",
    ) -> None:
        self.status = status
        self.category_hint = category_hint
        self.requests: list[dict[str, Any]] = []

    def classify(self, payload: Mapping[str, Any]) -> LabelClassificationOutcome:
        request = dict(payload)
        self.requests.append(request)
        if self.status != "SUCCEEDED":
            return LabelClassificationOutcome(
                status=self.status,  # type: ignore[arg-type]
                response_payload={},
                attempt_count=2,
                latency_ms=7,
                error_code="forced_failure",
            )
        label_ids = [item["labelId"] for item in request["labels"]]
        response = {
            **{key: value for key, value in request.items() if key != "labels"},
            "groups": [
                {
                    "groupId": "group-design",
                    "labelIds": label_ids,
                    "normalizedName": "디자인",
                    "entityKind": "MERCHANT",
                    "categoryHint": self.category_hint,
                    "essentialHint": True,
                    "confidence": "HIGH",
                    "reason": "법인격 표기만 다른 동일 상호입니다.",
                }
            ],
            "ungrouped": [],
        }
        return LabelClassificationOutcome(
            status="SUCCEEDED",
            response_payload=response,
            attempt_count=1,
            latency_ms=5,
        )


def _repository() -> FlowGuardRepository:
    return FlowGuardRepository("sqlite:///:memory:")


def test_off_mode_is_byte_for_byte_equivalent_and_never_calls_classifier() -> None:
    baseline_repository = _repository()
    disabled_repository = _repository()

    baseline = DataService(baseline_repository).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    disabled = DataService(
        disabled_repository,
        classification_mode="off",
        classification_client=MustNotCallClassifier(),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert disabled == baseline
    assert disabled_repository.current_state("user-1") == baseline_repository.current_state(
        "user-1"
    )
    assert disabled_repository.latest_classification_run("user-1") is None


def test_unset_feature_flag_preserves_the_existing_upload_api_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLOWGUARD_AI_CLASSIFICATION", raising=False)
    repository = _repository()

    with TestClient(
        create_app(repository, classification_client=MustNotCallClassifier())
    ) as client:
        response = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("transactions.csv", CSV_WITH_LABEL_VARIANTS, "text/csv")},
        )

    assert response.status_code == 201
    body = response.json()
    assert "classification_status" not in body
    assert "classification_summary" not in body
    assert "import_id" not in body


def test_on_mode_sends_only_redacted_labels_then_applies_validated_grouping() -> None:
    repository = _repository()
    classifier = MergingClassifier()
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
    )

    imported = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert imported["classification_status"] == "SUCCEEDED"
    assert imported["classification_summary"] == {
        "source": "AI",
        "applied": True,
        "original_label_count": 2,
        "grouped_entity_count": 1,
        "merged_label_count": 1,
    }
    assert len(imported["candidates"]) == 1
    candidate = imported["candidates"][0]
    assert candidate["classification_group"]["can_split"] is True
    assert candidate["proposed_record"]["event_type"] == "RENT"
    assert candidate["proposed_record"]["is_essential"] is True

    request = classifier.requests[0]
    assert request["schemaVersion"] == "1.2"
    assert set(request["labels"][0]) == {"labelId", "text", "direction", "occurrences"}
    assert all(
        not any(character.isdigit() for character in item["text"]) for item in request["labels"]
    )
    serialized = str(request["labels"])
    for forbidden in ("500000", "2026-", "t1", "account_id", "transaction_id"):
        assert forbidden not in serialized

    audit = repository.latest_classification_run("user-1")
    assert audit is not None
    assert audit["status"] == "SUCCEEDED"
    assert audit["applied"] is True
    assert "labels" not in audit["response_payload"]


@pytest.mark.parametrize("status", ["REJECTED", "FAILED"])
def test_rejected_or_failed_classification_preserves_deterministic_candidates(
    status: str,
) -> None:
    baseline = DataService(_repository()).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    repository = _repository()
    classified = DataService(
        repository,
        classification_mode="on",
        classification_client=MergingClassifier(status=status),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert classified["classification_status"] == status
    assert classified["classification_summary"]["source"] == "DETERMINISTIC_FALLBACK"
    assert classified["candidates"] == baseline["candidates"]
    audit = repository.latest_classification_run("user-1")
    assert audit is not None
    assert audit["status"] == status
    assert audit["fallback_used"] is True


def test_shadow_mode_records_but_does_not_apply_grouping() -> None:
    baseline = DataService(_repository()).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    repository = _repository()
    shadow = DataService(
        repository,
        classification_mode="shadow",
        classification_client=MergingClassifier(),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert shadow["classification_status"] == "SUCCEEDED"
    assert shadow["classification_summary"]["source"] == "AI_SHADOW"
    assert shadow["classification_summary"]["applied"] is False
    assert shadow["candidates"] == baseline["candidates"]
    audit = repository.latest_classification_run("user-1")
    assert audit is not None
    assert audit["applied"] is False


def test_successful_classification_is_reused_without_another_client_call() -> None:
    repository = _repository()
    classifier = MergingClassifier()
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
    )

    first = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    second = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert len(classifier.requests) == 1
    assert second["import_id"] == first["import_id"]
    assert second["candidates"] == first["candidates"]


def test_failed_classification_is_retried_with_a_new_attempt_key() -> None:
    repository = _repository()
    classifier = MergingClassifier(status="FAILED")
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
    )

    first = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    classifier.status = "SUCCEEDED"
    second = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert first["classification_status"] == "FAILED"
    assert second["classification_status"] == "SUCCEEDED"
    assert first["import_id"] == second["import_id"]
    assert len(classifier.requests) == 2
    assert classifier.requests[0]["idempotencyKey"] != classifier.requests[1]["idempotencyKey"]
    assert ":retry-classification-" in classifier.requests[1]["idempotencyKey"]


def test_atomic_classification_completion_failure_falls_back_without_losing_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = DataService(_repository()).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    repository = _repository()
    original_apply = repository.apply_record_bundle
    failed_once = False

    def fail_classification_completion(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal failed_once
        if kwargs.get("classification_completion") is not None and not failed_once:
            failed_once = True
            raise StorageConflict("forced atomic completion failure")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repository, "apply_record_bundle", fail_classification_completion)
    imported = DataService(
        repository,
        classification_mode="on",
        classification_client=MergingClassifier(),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert imported["classification_status"] == "FAILED"
    assert imported["classification_summary"]["source"] == "DETERMINISTIC_FALLBACK"
    assert imported["candidates"] == baseline["candidates"]
    audit = repository.latest_classification_run("user-1")
    assert audit is not None
    assert audit["status"] == "FAILED"
    assert audit["applied"] is False
    assert audit["error_code"] == "import_persistence_failed"


def test_losing_completion_reuses_committed_ai_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository()
    original_apply = repository.apply_record_bundle
    committed_by_winner = False

    def commit_then_report_conflict(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal committed_by_winner
        if kwargs.get("classification_completion") is not None and not committed_by_winner:
            committed_by_winner = True
            original_apply(*args, **kwargs)
            raise StorageConflict("simulated stale completion loser")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repository, "apply_record_bundle", commit_then_report_conflict)
    imported = DataService(
        repository,
        classification_mode="on",
        classification_client=MergingClassifier(),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert committed_by_winner is True
    assert imported["classification_status"] == "SUCCEEDED"
    assert imported["classification_summary"]["source"] == "AI"
    assert imported["classification_summary"]["applied"] is True
    assert len(imported["candidates"]) == 1
    assert imported["candidates"][0]["classification_group"]["source"] == "AI"
    stored_candidates = repository.list_records("user-1", "candidates")
    assert len(stored_candidates) == 1
    assert stored_candidates[0]["classification_group"]["source"] == "AI"
    audit = repository.latest_classification_run("user-1")
    assert audit is not None
    assert audit["status"] == "SUCCEEDED"
    assert audit["applied"] is True


@pytest.mark.parametrize(
    ("outcome_status", "winner_status"),
    [("FAILED", "REJECTED"), ("REJECTED", "FAILED")],
)
def test_terminal_update_loser_reuses_failed_or_rejected_winner(
    monkeypatch: pytest.MonkeyPatch,
    outcome_status: str,
    winner_status: str,
) -> None:
    repository = _repository()
    original_update = repository.update_classification_run
    winner_written = False

    def update_after_terminal_winner(
        classification_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal winner_written
        if not winner_written:
            winner_written = True
            original_update(
                classification_id,
                status=winner_status,
                applied=False,
                attempt_count=1,
                fallback_used=True,
                latency_ms=3,
                error_code=f"winner_{winner_status.lower()}",
            )
        return original_update(classification_id, **kwargs)

    monkeypatch.setattr(repository, "update_classification_run", update_after_terminal_winner)
    imported = DataService(
        repository,
        classification_mode="on",
        classification_client=MergingClassifier(status=outcome_status),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert imported["classification_status"] == winner_status
    assert imported["classification_summary"]["source"] == "DETERMINISTIC_FALLBACK"
    audit = repository.latest_classification_run("user-1")
    assert audit is not None
    assert audit["status"] == winner_status
    assert audit["applied"] is False


@pytest.mark.parametrize(
    ("mode", "status"),
    [("on", "FAILED"), ("on", "REJECTED"), ("shadow", "SUCCEEDED")],
)
def test_reset_cancels_non_applied_classified_import_without_restoring_data(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    status: str,
) -> None:
    repository = _repository()
    original_apply = repository.apply_record_bundle
    reset_before_save = False

    def apply_after_reset(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal reset_before_save
        completion = kwargs.get("classification_completion")
        if completion is not None and not reset_before_save:
            assert completion["status"] == status
            assert completion["applied"] is False
            reset_before_save = True
            repository.reset_user_data("user-1")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repository, "apply_record_bundle", apply_after_reset)
    service = DataService(
        repository,
        classification_mode=mode,  # type: ignore[arg-type]
        classification_client=MergingClassifier(status=status),
    )

    with pytest.raises(ServiceError) as exc_info:
        service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert reset_before_save is True
    assert exc_info.value.code == "IMPORT_STATE_CHANGED"
    assert repository.list_records("user-1", "transactions") == []
    assert repository.list_records("user-1", "candidates") == []
    assert repository.latest_classification_run("user-1") is None


def test_reused_applied_classification_cannot_restore_data_after_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository()
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=MergingClassifier(),
    )
    service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    original_apply = repository.apply_record_bundle
    reset_before_save = False

    def apply_after_reset(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal reset_before_save
        if kwargs.get("classification_completion") is not None and not reset_before_save:
            reset_before_save = True
            repository.reset_user_data("user-1")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repository, "apply_record_bundle", apply_after_reset)

    with pytest.raises(ServiceError) as exc_info:
        service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert reset_before_save is True
    assert exc_info.value.code == "IMPORT_STATE_CHANGED"
    assert repository.list_records("user-1", "transactions") == []
    assert repository.list_records("user-1", "candidates") == []
    assert repository.latest_classification_run("user-1") is None


def test_shadow_and_on_use_separate_logical_imports() -> None:
    repository = _repository()
    classifier = MergingClassifier()
    shadow = DataService(
        repository,
        classification_mode="shadow",
        classification_client=classifier,
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    applied = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert shadow["import_id"] != applied["import_id"]
    assert shadow["classification_summary"]["applied"] is False
    assert applied["classification_summary"]["applied"] is True
    assert len(classifier.requests) == 2


def test_locale_change_creates_a_new_logical_classification() -> None:
    repository = _repository()
    classifier = MergingClassifier()
    korean = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
        classification_locale="ko-KR",
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    english = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
        classification_locale="en-US",
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert korean["import_id"] != english["import_id"]
    assert [request["locale"] for request in classifier.requests] == ["ko-KR", "en-US"]


@pytest.mark.parametrize("category_hint", ["CARD_BILL", "INSTALLMENT_PAYMENT"])
def test_unlinked_card_or_installment_hint_is_not_promoted_to_cashflow_event_type(
    category_hint: str,
) -> None:
    imported = DataService(
        _repository(),
        classification_mode="on",
        classification_client=MergingClassifier(category_hint=category_hint),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    candidate = imported["candidates"][0]
    assert candidate["classification_group"]["category_hint"] == category_hint
    assert candidate["proposed_record"]["event_type"] == "RENT"


def test_user_can_split_an_ai_group_back_to_exact_label_candidates() -> None:
    repository = _repository()
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=MergingClassifier(),
    )
    imported = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    merged = imported["candidates"][0]

    split = service.split_candidate_group("user-1", merged["candidate_id"])

    assert split["split"] is True
    assert len(split["candidates"]) == 2
    assert all("classification_group" not in item for item in split["candidates"])
    assert (
        repository.get_record("user-1", "candidates", merged["candidate_id"])["status"]
        == "REJECTED"
    )
    pending_ids = {
        item["candidate_id"]
        for item in repository.list_records("user-1", "candidates")
        if item["status"] == "PENDING"
    }
    assert pending_ids == {item["candidate_id"] for item in split["candidates"]}

    stored_merged = repository.get_record("user-1", "candidates", merged["candidate_id"])
    assert stored_merged["classification_group"]["user_split"] is True
    assert stored_merged["classification_group"]["can_split"] is False

    with pytest.raises(ServiceError) as exc_info:
        service.decide_candidate(
            "user-1",
            merged["candidate_id"],
            decision="CONFIRMED",
        )
    assert exc_info.value.code == "CANDIDATE_GROUP_SUPERSEDED"


def test_user_split_survives_reimport_and_an_added_transaction() -> None:
    repository = _repository()
    classifier = MergingClassifier()
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
    )
    first = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    service.split_candidate_group("user-1", first["candidates"][0]["candidate_id"])

    same_input = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    extended_input = CSV_WITH_LABEL_VARIANTS + (
        "t5,a,2026-05-01T09:00:00+09:00,OUTFLOW,500000,월세 05월,디자인 주식회사\n"
    )
    extended = service.import_csv("user-1", extended_input)

    assert len(classifier.requests) == 2
    assert len(same_input["candidates"]) == 2
    assert len(extended["candidates"]) == 2
    assert all("classification_group" not in item for item in extended["candidates"])
    assert same_input["classification_summary"]["user_split_group_count"] == 1
    assert extended["classification_summary"]["user_split_group_count"] == 1


def test_reimport_reconciles_again_when_user_splits_during_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository()
    classifier = MergingClassifier()
    service = DataService(
        repository,
        classification_mode="on",
        classification_client=classifier,
    )
    first = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    merged_id = first["candidates"][0]["candidate_id"]
    original_apply = repository.apply_record_bundle
    split_during_apply = False

    def apply_after_split(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal split_during_apply
        if kwargs.get("expected_revision") is not None and not split_during_apply:
            split_during_apply = True
            service.split_candidate_group("user-1", merged_id)
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repository, "apply_record_bundle", apply_after_split)

    imported = service.import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    assert split_during_apply is True
    assert len(classifier.requests) == 1
    assert len(imported["candidates"]) == 2
    marker = repository.get_record("user-1", "candidates", merged_id)
    assert marker["classification_group"]["user_split"] is True
    assert marker["status"] == "REJECTED"


def test_revision_retry_restarts_from_pristine_normalized_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = DataService(_repository()).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)
    candidate = baseline["candidates"][0]
    repository = _repository()
    repository.upsert_records(
        "user-1",
        "candidates",
        [{**candidate, "status": "CONFIRMED"}],
    )
    original_apply = repository.apply_record_bundle
    changed_during_first_attempt = False

    def apply_after_candidate_change(*args: Any, **kwargs: Any) -> dict[str, int]:
        nonlocal changed_during_first_attempt
        if kwargs.get("expected_revision") is not None and not changed_during_first_attempt:
            changed_during_first_attempt = True
            repository.upsert_records(
                "user-1",
                "candidates",
                [{**candidate, "status": "PENDING"}],
            )
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repository, "apply_record_bundle", apply_after_candidate_change)
    imported = DataService(
        repository,
        classification_mode="shadow",
        classification_client=MergingClassifier(),
    ).import_csv("user-1", CSV_WITH_LABEL_VARIANTS)

    returned = next(
        item for item in imported["candidates"] if item["candidate_id"] == candidate["candidate_id"]
    )
    stored = repository.get_record("user-1", "candidates", candidate["candidate_id"])
    assert changed_during_first_attempt is True
    assert returned["status"] == "PENDING"
    assert stored["status"] == "PENDING"


def test_split_candidate_api_returns_new_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLOWGUARD_AI_CLASSIFICATION", "on")
    repository = _repository()
    with TestClient(create_app(repository, classification_client=MergingClassifier())) as client:
        imported = client.post(
            "/api/v1/imports/transactions",
            files={"file": ("transactions.csv", CSV_WITH_LABEL_VARIANTS, "text/csv")},
        )
        candidate_id = imported.json()["candidates"][0]["candidate_id"]

        response = client.post(f"/api/v1/candidates/{candidate_id}/split")

    assert response.status_code == 200
    assert response.json()["split"] is True
    assert response.json()["analysis_required"] is True
    assert response.json()["revision"] == "rev-2"
