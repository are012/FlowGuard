from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest

import flowguard.services.tools as tools_module
from flowguard.domain import FinancialSnapshot
from flowguard.services.errors import ServiceError
from flowguard.services.tools import CoreToolService
from flowguard.storage import FlowGuardRepository, RecordNotFound

AS_OF = datetime(2026, 1, 31, 9, tzinfo=UTC)


def _snapshot(**updates: object) -> FinancialSnapshot:
    values: dict[str, object] = {
        "snapshot_id": "snapshot-1",
        "user_id": "user-1",
        "as_of": AS_OF,
        "accounts": [
            {
                "account_id": "account-1",
                "name": "결제계좌",
                "account_type": "CHECKING",
                "balance": 1_000_000,
                "is_payment_account": True,
                "updated_at": AS_OF,
            }
        ],
    }
    values.update(updates)
    return FinancialSnapshot.model_validate(values)


def _service(snapshot: FinancialSnapshot | None = None) -> tuple[CoreToolService, Mock]:
    repository = Mock(spec=FlowGuardRepository)
    repository.get_snapshot.return_value = (snapshot or _snapshot()).model_dump(mode="json")
    return CoreToolService(repository), repository


def test_event_query_rejects_reversed_range_and_unknown_filters() -> None:
    service, repository = _service()

    with pytest.raises(ServiceError) as reversed_range:
        service.query_financial_events(
            "snapshot-1",
            date_from=date(2026, 2, 2),
            date_to=date(2026, 2, 1),
        )

    assert reversed_range.value.code == "INVALID_FINANCIAL_EVENT"
    assert reversed_range.value.details == {
        "date_from": "2026-02-02",
        "date_to": "2026-02-01",
    }
    repository.get_snapshot.assert_not_called()

    with pytest.raises(ServiceError) as invalid_filters:
        service.query_financial_events(
            "snapshot-1",
            date_from=date(2026, 2, 1),
            date_to=date(2026, 2, 2),
            directions=["SIDEWAYS"],
            event_types=["LOAN"],
        )

    assert invalid_filters.value.code == "INVALID_FINANCIAL_EVENT"
    assert invalid_filters.value.details == {
        "invalid_directions": ["SIDEWAYS"],
        "invalid_event_types": ["LOAN"],
    }


def test_cashflow_tools_reject_unsupported_horizons_and_scenarios() -> None:
    service, repository = _service()

    with pytest.raises(ServiceError) as cashflow_horizon:
        service.simulate_cashflow("snapshot-1", horizon_days=30)
    assert cashflow_horizon.value.code == "INVALID_FINANCIAL_EVENT"
    assert cashflow_horizon.value.details == {"horizon_days": 30}

    with pytest.raises(ServiceError) as scenario:
        service.simulate_cashflow(
            "snapshot-1",
            scenario={"receivable_delay_mode": "MONTE_CARLO"},
        )
    assert scenario.value.code == "INVALID_FINANCIAL_EVENT"

    with pytest.raises(ServiceError) as safe_to_spend_horizon:
        service.calculate_safe_to_spend("snapshot-1", horizon_days=92)
    assert safe_to_spend_horizon.value.code == "INVALID_FINANCIAL_EVENT"
    assert repository.get_snapshot.call_count == 0


def test_snapshot_lookup_and_validation_fail_closed() -> None:
    missing_repository = Mock(spec=FlowGuardRepository)
    missing_repository.get_snapshot.side_effect = RecordNotFound("missing")

    with pytest.raises(ServiceError) as missing:
        CoreToolService(missing_repository).get_financial_context("snapshot-missing")

    assert missing.value.code == "SNAPSHOT_NOT_FOUND"
    assert missing.value.http_status == 404
    assert missing.value.details == {"snapshot_id": "snapshot-missing"}

    invalid_repository = Mock(spec=FlowGuardRepository)
    invalid_repository.get_snapshot.return_value = {"snapshot_id": "snapshot-invalid"}

    with pytest.raises(ServiceError) as invalid:
        CoreToolService(invalid_repository).get_financial_context("snapshot-invalid")

    assert invalid.value.code == "INSUFFICIENT_DATA"
    assert invalid.value.http_status == 422
    assert invalid.value.details["validation_errors"]
    assert {"type", "location", "message"} == set(invalid.value.details["validation_errors"][0])


def test_counterparty_and_action_schema_errors_are_public_and_structured() -> None:
    service, _ = _service()

    with pytest.raises(ServiceError) as evidence:
        service.get_counterparty_evidence("snapshot-1", "unknown-client")
    assert evidence.value.code == "INSUFFICIENT_DATA"
    assert evidence.value.details == {"counterparty_id": "unknown-client"}

    with pytest.raises(ServiceError) as action:
        service.evaluate_action_plan(
            "snapshot-1",
            actions=[{"type": "borrow_money", "parameters": {}}],
        )
    assert action.value.code == "ACTION_SCHEMA_INVALID"
    errors = action.value.details["validation_errors"]
    assert errors
    assert all(set(error) == {"type", "location", "message"} for error in errors)

    with pytest.raises(ServiceError) as evaluation:
        service.validate_financial_policy(
            "snapshot-1",
            actions=[],
            evaluation_result={"unexpected": "payload"},
        )
    assert evaluation.value.code == "ACTION_SCHEMA_INVALID"
    assert evaluation.value.details["validation_errors"]


CoreInvocation = Callable[[CoreToolService], object]

CORE_FAILURE_CASES: tuple[tuple[str, CoreInvocation, str], ...] = (
    (
        "simulate_cashflow",
        lambda service: service.simulate_cashflow("snapshot-1"),
        "CASHFLOW_SIMULATION_FAILED",
    ),
    (
        "calculate_safe_to_spend",
        lambda service: service.calculate_safe_to_spend("snapshot-1"),
        "SAFE_TO_SPEND_CALCULATION_FAILED",
    ),
    (
        "evaluate_action_plan",
        lambda service: service.evaluate_action_plan("snapshot-1", actions=[]),
        "ACTION_NOT_FEASIBLE",
    ),
    (
        "validate_financial_policy",
        lambda service: service.validate_financial_policy("snapshot-1", actions=[]),
        "POLICY_VALIDATION_FAILED",
    ),
    (
        "core_apply_actions_virtual",
        lambda service: service.apply_actions_virtual(
            "snapshot-1",
            actions=[],
            new_snapshot_id="snapshot-virtual",
        ),
        "ACTION_NOT_FEASIBLE",
    ),
)


@pytest.mark.parametrize(("core_name", "invoke", "expected_code"), CORE_FAILURE_CASES)
def test_unexpected_core_failures_are_translated_to_stable_service_errors(
    monkeypatch: pytest.MonkeyPatch,
    core_name: str,
    invoke: CoreInvocation,
    expected_code: str,
) -> None:
    service, _ = _service()
    core_failure = RuntimeError("private core failure")
    monkeypatch.setattr(tools_module, core_name, Mock(side_effect=core_failure))

    with pytest.raises(ServiceError) as caught:
        invoke(service)

    assert caught.value.code == expected_code
    assert caught.value.http_status == 422
    assert caught.value.retryable is False
    assert caught.value.__cause__ is core_failure
    assert "private core failure" not in caught.value.message


@pytest.mark.parametrize(("core_name", "invoke", "_expected_code"), CORE_FAILURE_CASES)
def test_existing_service_errors_from_core_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
    core_name: str,
    invoke: CoreInvocation,
    _expected_code: str,
) -> None:
    service, _ = _service()
    expected = ServiceError(
        "UPSTREAM_CONTRACT", "이미 안전하게 변환된 오류입니다.", http_status=409
    )
    monkeypatch.setattr(tools_module, core_name, Mock(side_effect=expected))

    with pytest.raises(ServiceError) as caught:
        invoke(service)

    assert caught.value is expected


def test_event_normalization_skips_closed_records_and_prevents_double_counting() -> None:
    snapshot = _snapshot(
        counterparties=[
            {
                "counterparty_id": "client-1",
                "name": "거래처",
                "updated_at": AS_OF,
            }
        ],
        scheduled_events=[
            {
                "event_id": "scheduled-receivable",
                "event_type": "RECEIVABLE",
                "direction": "INFLOW",
                "amount": 200_000,
                "expected_date": date(2026, 2, 2),
                "account_id": "account-1",
                "counterparty_id": "client-1",
                "source_reference_id": "external-receivable",
            }
        ],
        receivables=[
            {
                "receivable_id": "receivable-closed",
                "counterparty_id": "client-1",
                "amount": 100_000,
                "expected_date": date(2026, 2, 1),
                "status": "RECEIVED",
                "destination_account_id": "account-1",
                "updated_at": AS_OF,
            },
            {
                "receivable_id": "receivable-duplicate",
                "counterparty_id": "client-1",
                "amount": 200_000,
                "expected_date": date(2026, 2, 2),
                "status": "ESTIMATED",
                "destination_account_id": "account-1",
                "updated_at": AS_OF,
            },
        ],
        cards=[
            {
                "card_id": "card-1",
                "name": "생활비 카드",
                "payment_account_id": "account-1",
                "payment_day": 28,
                "current_billing_amount": 300_000,
                "billing_date": date(2026, 2, 28),
                "updated_at": AS_OF,
            }
        ],
        installment_plans=[
            {
                "installment_plan_id": "installment-active",
                "card_id": "card-1",
                "original_amount": 200_000,
                "monthly_payment": 100_000,
                "total_months": 2,
                "remaining_months": 2,
                "next_payment_date": date(2026, 2, 28),
                "status": "ACTIVE",
            },
            {
                "installment_plan_id": "installment-closed",
                "card_id": "card-1",
                "original_amount": 100_000,
                "monthly_payment": 100_000,
                "total_months": 1,
                "remaining_months": 0,
                "next_payment_date": date(2026, 2, 28),
                "status": "COMPLETED",
            },
        ],
    )

    events = CoreToolService._normalized_events(snapshot)
    event_ids = {event["event_id"] for event in events}

    assert "receivable:receivable-closed" not in event_ids
    assert "receivable:receivable-duplicate" not in event_ids
    assert "scheduled-receivable" in event_ids
    assert "installment:installment-active:1" not in event_ids
    assert "installment:installment-active:2" in event_ids
    assert not any("installment-closed" in event_id for event_id in event_ids)


def test_event_normalization_rejects_installment_without_its_card() -> None:
    valid_snapshot = _snapshot(
        cards=[
            {
                "card_id": "missing-card",
                "name": "삭제 예정 카드",
                "payment_account_id": "account-1",
                "payment_day": 28,
                "billing_date": date(2026, 2, 28),
                "updated_at": AS_OF,
            }
        ],
        installment_plans=[
            {
                "installment_plan_id": "installment-orphan",
                "card_id": "missing-card",
                "original_amount": 100_000,
                "monthly_payment": 50_000,
                "total_months": 2,
                "remaining_months": 2,
                "next_payment_date": date(2026, 2, 28),
                "status": "ACTIVE",
            }
        ],
    )
    snapshot = valid_snapshot.model_copy(update={"cards": ()})

    with pytest.raises(ServiceError) as caught:
        CoreToolService._normalized_events(snapshot)

    assert caught.value.code == "INSUFFICIENT_DATA"
    assert caught.value.details == {"installment_plan_id": "installment-orphan"}
