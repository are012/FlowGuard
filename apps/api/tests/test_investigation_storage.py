from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from flowguard.services.observation_projection import project_observation
from flowguard.storage import (
    FlowGuardRepository,
    RecordNotFound,
    StorageConflict,
    StorageError,
)


@pytest.fixture
def repository() -> FlowGuardRepository:
    return FlowGuardRepository("sqlite:///:memory:")


def _run_values(
    repository: FlowGuardRepository,
    *,
    user_id: str = "user-1",
    suffix: str = "a",
) -> dict[str, Any]:
    snapshot = repository.create_snapshot(
        user_id,
        as_of=datetime(2026, 8, 2, tzinfo=UTC),
    )
    analysis = repository.create_analysis(user_id, trigger_type="MANUAL")
    repository.transition_analysis(
        analysis["analysis_id"],
        "SNAPSHOT_BUILDING",
        message="snapshot",
        snapshot_id=snapshot["snapshot_id"],
    )
    return {
        "analysis_id": analysis["analysis_id"],
        "user_id": user_id,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_revision": "rev-0",
        "request_id": f"investigation-request-{suffix}",
        "idempotency_key": f"investigation-key-{suffix}",
        "schema_version": "1.3",
        "contract_version": "1.3",
        "prompt_version": "invest-1",
        "mode": "SHADOW",
        "model_name": "test-model",
    }


def _params(**overrides: Any) -> dict[str, Any]:
    return {
        "counterpartyId": None,
        "dateFrom": None,
        "dateTo": None,
        "actionType": None,
        **overrides,
    }


def _counterparty_observation() -> dict[str, Any]:
    return {
        "tool": "get_counterparty_evidence",
        "params": _params(counterpartyId="counterparty-a"),
        "reason": "입금 예정 거래처의 지급 이력을 확인합니다.",
        "result": {
            "counterparty_id": "counterparty-a",
            "payment_history_count": 4,
            "on_time_rate": 0.75,
            "average_delay_days": 1.5,
            "median_delay_days": 1,
            "maximum_delay_days": 3,
            "recent_trend": "STABLE",
            "data_confidence": 0.8,
        },
    }


def _event_observation() -> dict[str, Any]:
    return {
        "tool": "query_financial_events",
        "params": _params(dateFrom="2026-08-02", dateTo="2026-10-31"),
        "reason": "예정된 필수 금융 이벤트를 확인합니다.",
        "result": {
            "events": [
                {
                    "event_id": "event-a",
                    "type": "RECEIVABLE",
                    "date": "2026-08-20",
                    "amount": 300_000,
                    "is_essential": True,
                }
            ]
        },
    }


def _turn(
    repository: FlowGuardRepository,
    investigation_id: str,
    sequence: int,
    *,
    request_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    phase = 1 if sequence == 1 else 2
    return repository.record_investigation_turn(
        investigation_id=investigation_id,
        turn_sequence=sequence,
        phase=phase,
        endpoint="/investigate/plan" if phase == 1 else "/investigate/conclude",
        status="SUCCEEDED",
        request_payload=request_payload or {"requestId": f"request-{sequence}"},
        response_payload={"requestId": f"request-{sequence}"},
        error_code=None,
        attempt_count=1,
        latency_ms=20 + sequence,
    )


def test_investigation_run_turns_and_completion_are_replayable(
    repository: FlowGuardRepository,
) -> None:
    values = _run_values(repository)
    first, created = repository.create_or_get_investigation_run(**values)
    second, created_again = repository.create_or_get_investigation_run(**values)

    assert created is True
    assert created_again is False
    assert second["investigation_id"] == first["investigation_id"]
    assert first["schema_version"] == "1.3"
    assert first["status"] == "IN_PROGRESS"

    turn_one = _turn(repository, first["investigation_id"], 1)
    conclude_request = {
        "requestId": "request-2",
        "observations": [_counterparty_observation()],
    }
    turn_two = _turn(
        repository,
        first["investigation_id"],
        2,
        request_payload=conclude_request,
    )
    repeated_turn = _turn(
        repository,
        first["investigation_id"],
        2,
        request_payload=conclude_request,
    )
    _turn(repository, first["investigation_id"], 3)

    assert repeated_turn["turn_id"] == turn_two["turn_id"]
    turns = repository.list_investigation_turns(first["investigation_id"])
    assert [turn["turn_sequence"] for turn in turns] == [1, 2, 3]
    assert turns[0]["turn_id"] == turn_one["turn_id"]

    observations = [_counterparty_observation(), _event_observation()]
    hypotheses = [
        {
            "type": "COUNTERPARTY_DELAY",
            "summary": "거래처 지급 변동이 유동성 위험을 키울 수 있습니다.",
            "priority": 1,
        }
    ]
    completed = repository.complete_investigation_run(
        first["investigation_id"],
        status="SUCCEEDED",
        observations=observations,
        hypotheses=hypotheses,
        priorities=["confirm_receivable", "transfer"],
        unresolved=["최근 지급 이력의 대표성을 확인하지 못했습니다."],
        tool_call_count=2,
        total_latency_ms=85,
        additional_investigation_requested=True,
    )
    observations[0]["result"]["payment_history_count"] = 999
    hypotheses[0]["summary"] = "mutated"

    stored = repository.get_investigation_run(first["investigation_id"])
    listed = repository.list_investigation_runs(
        "user-1",
        analysis_id=values["analysis_id"],
    )
    assert completed["status"] == "SUCCEEDED"
    assert completed["completed_at"] is not None
    assert stored["observations"][0]["result"]["payment_history_count"] == 4
    assert stored["hypotheses"][0]["summary"] != "mutated"
    assert [item["investigation_id"] for item in listed] == [first["investigation_id"]]

    repeated = repository.complete_investigation_run(
        first["investigation_id"],
        status="SUCCEEDED",
        observations=stored["observations"],
        hypotheses=stored["hypotheses"],
        priorities=stored["priorities"],
        unresolved=stored["unresolved"],
        tool_call_count=2,
        total_latency_ms=85,
        additional_investigation_requested=True,
    )
    assert repeated["completed_at"] == completed["completed_at"]


def test_investigation_turn_sequence_and_terminal_status_are_immutable(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))

    with pytest.raises(StorageConflict, match="recorded in order"):
        _turn(repository, run["investigation_id"], 2)
    _turn(repository, run["investigation_id"], 1)
    with pytest.raises(StorageConflict, match="recorded in order"):
        _turn(repository, run["investigation_id"], 3)
    with pytest.raises(StorageError, match="does not match its phase"):
        repository.record_investigation_turn(
            investigation_id=run["investigation_id"],
            turn_sequence=2,
            phase=1,
            endpoint="/investigate/plan",
            status="SUCCEEDED",
            request_payload={},
            response_payload={},
            error_code=None,
            attempt_count=1,
            latency_ms=1,
        )

    failed = repository.fail_investigation_run(
        run["investigation_id"],
        status="FAILED",
        observations=[_counterparty_observation()],
        tool_call_count=1,
        total_latency_ms=45,
        additional_investigation_requested=False,
        error_code="connection_failed",
    )
    assert failed["observations"] == [_counterparty_observation()]
    assert failed["hypotheses"] == []
    assert failed["priorities"] == []
    assert failed["unresolved"] == []

    with pytest.raises(StorageConflict, match="terminal status is immutable"):
        repository.complete_investigation_run(
            run["investigation_id"],
            status="PARTIAL",
            observations=[_counterparty_observation()],
            hypotheses=[],
            priorities=[],
            unresolved=[],
            tool_call_count=1,
            total_latency_ms=45,
            additional_investigation_requested=False,
            error_code="connection_failed",
        )
    with pytest.raises(StorageConflict, match="terminal status is immutable"):
        _turn(repository, run["investigation_id"], 2)


def test_duplicate_turn_with_different_payload_is_rejected(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))
    first = _turn(repository, run["investigation_id"], 1)

    with pytest.raises(StorageConflict, match="different fields"):
        repository.record_investigation_turn(
            investigation_id=run["investigation_id"],
            turn_sequence=1,
            phase=1,
            endpoint="/investigate/plan",
            status="FAILED",
            request_payload=first["request_payload"],
            response_payload={},
            error_code="late_failure",
            attempt_count=1,
            latency_ms=99,
        )


def test_identical_investigation_turn_is_concurrency_safe(tmp_path: Path) -> None:
    repository = FlowGuardRepository(f"sqlite:///{tmp_path / 'concurrent-turn.db'}")
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))
    barrier = Barrier(5)

    def record() -> str:
        barrier.wait()
        return _turn(repository, run["investigation_id"], 1)["turn_id"]

    with ThreadPoolExecutor(max_workers=5) as executor:
        turn_ids = list(executor.map(lambda _: record(), range(5)))

    assert len(set(turn_ids)) == 1
    assert len(repository.list_investigation_turns(run["investigation_id"])) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda observation: observation["result"]["events"][0].update(
            {"description": "raw description"}
        ),
        lambda observation: observation["result"]["events"][0].update(
            {"counterparty_id": "raw-counterparty"}
        ),
        lambda observation: observation["result"]["events"][0].update(
            {"secret_future_field": "secret"}
        ),
    ],
)
def test_investigation_audit_rejects_unprojected_observation_fields(
    repository: FlowGuardRepository,
    mutate,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))
    observation = _event_observation()
    mutate(observation)

    with pytest.raises(StorageError):
        repository.fail_investigation_run(
            run["investigation_id"],
            status="REJECTED",
            observations=[observation],
            tool_call_count=1,
            total_latency_ms=15,
            additional_investigation_requested=False,
            error_code="invalid_response",
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda observation: observation.update({"tool": 7}),
        lambda observation: observation.update({"reason": None}),
        lambda observation: observation["params"].pop("actionType"),
        lambda observation: observation["params"].update({"counterpartyId": "unexpected"}),
        lambda observation: observation["result"]["events"][0].update({"amount": True}),
        lambda observation: observation["result"]["events"][0].update({"date": "not-a-date"}),
    ],
)
def test_investigation_audit_validates_projected_observation_types(
    repository: FlowGuardRepository,
    mutate,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))
    observation = _event_observation()
    mutate(observation)

    with pytest.raises(StorageError):
        repository.complete_investigation_run(
            run["investigation_id"],
            status="PARTIAL",
            observations=[observation],
            hypotheses=[],
            priorities=[],
            unresolved=[],
            tool_call_count=1,
            total_latency_ms=15,
            additional_investigation_requested=False,
            error_code="invalid_observation",
        )


def test_storage_projection_contract_matches_the_public_projectors(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))
    observations = [
        {
            **_counterparty_observation(),
            "result": project_observation(
                "get_counterparty_evidence",
                {
                    **_counterparty_observation()["result"],
                    "counterparty_name": "excluded",
                },
            ),
        },
        {
            **_event_observation(),
            "result": project_observation(
                "query_financial_events",
                {
                    "events": [
                        {
                            "event_id": "event-a",
                            "event_type": "RECEIVABLE",
                            "expected_date": "2026-08-20",
                            "amount": 300_000,
                            "is_essential": True,
                            "description": "excluded",
                        }
                    ]
                },
            ),
        },
        {
            "tool": "get_financial_context",
            "params": _params(),
            "reason": "재무 집계 맥락을 확인합니다.",
            "result": project_observation(
                "get_financial_context",
                {
                    "accounts": [{"account_id": "secret"}],
                    "protected_funds": [{"amount": 100}],
                    "scheduled_events": [{"amount": 200, "is_essential": True}],
                },
            ),
        },
    ]

    completed = repository.complete_investigation_run(
        run["investigation_id"],
        status="SUCCEEDED",
        observations=observations,
        hypotheses=[
            {
                "type": "COUNTERPARTY_DELAY",
                "summary": "거래처 지급 변동이 위험을 키울 수 있습니다.",
                "priority": 1,
            }
        ],
        priorities=[],
        unresolved=[],
        tool_call_count=3,
        total_latency_ms=30,
        additional_investigation_requested=False,
    )

    assert completed["observations"] == observations


@pytest.mark.parametrize(
    "bad_value",
    [
        "transfer",
        {"priority": "transfer"},
    ],
)
def test_investigation_json_lists_reject_unordered_iterables(
    repository: FlowGuardRepository,
    bad_value: Any,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))

    with pytest.raises(StorageError, match="ordered list"):
        repository.complete_investigation_run(
            run["investigation_id"],
            status="PARTIAL",
            observations=[],
            hypotheses=[],
            priorities=bad_value,
            unresolved=[],
            tool_call_count=0,
            total_latency_ms=1,
            additional_investigation_requested=False,
            error_code="no_conclusion",
        )


def test_investigation_audit_payloads_are_json_safe_deep_copies(
    repository: FlowGuardRepository,
) -> None:
    run, _ = repository.create_or_get_investigation_run(**_run_values(repository))
    request_payload = {"requestId": "request-1", "nested": {"value": "original"}}
    _turn(
        repository,
        run["investigation_id"],
        1,
        request_payload=request_payload,
    )
    request_payload["nested"]["value"] = "mutated"
    assert (
        repository.list_investigation_turns(run["investigation_id"])[0]["request_payload"][
            "nested"
        ]["value"]
        == "original"
    )

    with pytest.raises(StorageError, match="JSON-safe"):
        repository.record_investigation_turn(
            investigation_id=run["investigation_id"],
            turn_sequence=2,
            phase=2,
            endpoint="/investigate/conclude",
            status="FAILED",
            request_payload={"value": float("nan")},
            response_payload={},
            error_code="invalid_json",
            attempt_count=1,
            latency_ms=1,
        )


def test_investigation_run_idempotency_is_concurrency_safe(tmp_path: Path) -> None:
    repository = FlowGuardRepository(f"sqlite:///{tmp_path / 'concurrent.db'}")
    values = _run_values(repository)
    barrier = Barrier(5)

    def create() -> tuple[str, bool]:
        barrier.wait()
        run, created = repository.create_or_get_investigation_run(**values)
        return run["investigation_id"], created

    with ThreadPoolExecutor(max_workers=5) as executor:
        outcomes = list(executor.map(lambda _: create(), range(5)))

    assert len({investigation_id for investigation_id, _created in outcomes}) == 1
    assert sum(created for _investigation_id, created in outcomes) == 1
    assert len(repository.list_investigation_runs("user-1")) == 1


def test_reset_user_data_deletes_investigation_runs_and_turns_only_for_that_user(
    repository: FlowGuardRepository,
) -> None:
    first, _ = repository.create_or_get_investigation_run(
        **_run_values(repository, user_id="user-1", suffix="user-1")
    )
    second, _ = repository.create_or_get_investigation_run(
        **_run_values(repository, user_id="user-2", suffix="user-2")
    )
    _turn(repository, first["investigation_id"], 1)
    _turn(repository, second["investigation_id"], 1)

    repository.reset_user_data("user-1")

    with pytest.raises(RecordNotFound):
        repository.get_investigation_run(first["investigation_id"])
    with pytest.raises(RecordNotFound):
        repository.list_investigation_turns(first["investigation_id"])
    assert repository.get_investigation_run(second["investigation_id"])["user_id"] == "user-2"
    assert len(repository.list_investigation_turns(second["investigation_id"])) == 1


def test_investigation_run_identity_mode_and_snapshot_are_guarded(
    repository: FlowGuardRepository,
) -> None:
    values = _run_values(repository)
    repository.create_or_get_investigation_run(**values)

    with pytest.raises(StorageConflict, match="different investigation fields"):
        repository.create_or_get_investigation_run(**{**values, "request_id": "different-request"})
    with pytest.raises(StorageError, match="unsupported investigation mode"):
        repository.create_or_get_investigation_run(
            **{**values, "idempotency_key": "off-key", "mode": "OFF"}
        )

    other = _run_values(repository, suffix="other")
    with pytest.raises(StorageConflict, match="snapshot does not match"):
        repository.create_or_get_investigation_run(
            **{
                **other,
                "idempotency_key": "mismatched-snapshot",
                "snapshot_id": values["snapshot_id"],
            }
        )
