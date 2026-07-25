"""Application-facing implementations of the seven FlowGuard MCP tools."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from pydantic import TypeAdapter, ValidationError

from flowguard.core import (
    apply_actions_virtual as core_apply_actions_virtual,
)
from flowguard.core import (
    calculate_safe_to_spend,
    evaluate_action_plan,
    get_counterparty_evidence,
    simulate_cashflow,
    validate_financial_policy,
)
from flowguard.domain import (
    Action,
    ActionEvaluation,
    CashflowAnalysis,
    Direction,
    EventType,
    FinancialSnapshot,
)
from flowguard.storage import FlowGuardRepository, RecordNotFound, jsonable

from .errors import ServiceError

ACTION_LIST_ADAPTER = TypeAdapter(list[Action])


class CoreToolService:
    """Load immutable snapshots and delegate all financial arithmetic to core."""

    def __init__(self, repository: FlowGuardRepository) -> None:
        self.repository = repository

    def get_financial_context(self, snapshot_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(snapshot_id)
        context = {
            key: value
            for key, value in snapshot.model_dump(mode="json").items()
            if key
            in {
                "snapshot_id",
                "as_of",
                "accounts",
                "cards",
                "receivables",
                "scheduled_events",
                "installment_plans",
                "protected_funds",
                "data_quality",
            }
        }
        context["accounts"] = [
            {
                **account.model_dump(mode="json"),
                "available_balance": account.available_balance,
            }
            for account in snapshot.accounts
        ]
        return context

    def get_counterparty_evidence(self, snapshot_id: str, counterparty_id: str) -> dict[str, Any]:
        snapshot = self._snapshot(snapshot_id)
        try:
            return jsonable(get_counterparty_evidence(snapshot, counterparty_id))
        except ValueError as exc:
            raise ServiceError(
                "INSUFFICIENT_DATA",
                "거래처 지급 근거를 계산할 수 없습니다.",
                details={"counterparty_id": counterparty_id},
                http_status=422,
            ) from exc

    def query_financial_events(
        self,
        snapshot_id: str,
        *,
        date_from: date,
        date_to: date,
        directions: list[str] | None = None,
        event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        if date_to < date_from:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "조회 종료일은 시작일보다 빠를 수 없습니다.",
                details={"date_from": date_from.isoformat(), "date_to": date_to.isoformat()},
                http_status=422,
            )
        snapshot = self._snapshot(snapshot_id)
        invalid_directions = sorted(set(directions or ()) - {item.value for item in Direction})
        invalid_event_types = sorted(set(event_types or ()) - {item.value for item in EventType})
        if invalid_directions or invalid_event_types:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "금융 이벤트 조회 필터가 올바르지 않습니다.",
                details={
                    "invalid_directions": invalid_directions,
                    "invalid_event_types": invalid_event_types,
                },
                http_status=422,
            )
        direction_filter = set(directions or ())
        event_type_filter = set(event_types or ())
        events = [
            event
            for event in self._normalized_events(snapshot)
            if date_from <= date.fromisoformat(event["expected_date"]) <= date_to
            and (not direction_filter or event["direction"] in direction_filter)
            and (not event_type_filter or event["event_type"] in event_type_filter)
        ]
        return {"snapshot_id": snapshot_id, "events": events}

    def simulate_cashflow(
        self,
        snapshot_id: str,
        *,
        horizon_days: int = 91,
        scenario: dict[str, Any] | None = None,
        seed: int = 42,
    ) -> dict[str, Any]:
        if horizon_days != 91:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "MVP 분석기간은 91일로 고정되어 있습니다.",
                details={"horizon_days": horizon_days},
                http_status=422,
            )
        if scenario not in (
            None,
            {},
            {"receivable_delay_mode": "FIXED_SCENARIOS"},
        ):
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "지원하지 않는 현금흐름 시나리오입니다.",
                details={"scenario": scenario},
                http_status=422,
            )
        try:
            return jsonable(simulate_cashflow(self._snapshot(snapshot_id), seed=seed))
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                "CASHFLOW_SIMULATION_FAILED",
                "현금흐름 계산에 실패했습니다.",
                retryable=False,
                http_status=422,
            ) from exc

    @staticmethod
    def _normalized_events(snapshot: FinancialSnapshot) -> list[dict[str, Any]]:
        events = [event.model_dump(mode="json") for event in snapshot.scheduled_events]

        def already_present(
            *,
            source_reference_id: str,
            event_type: str,
            expected_date: date,
            account_id: str,
            amount: int,
        ) -> bool:
            return any(
                (
                    event.get("source_reference_id") == source_reference_id
                    and event["event_type"] == event_type
                    and event["expected_date"] == expected_date.isoformat()
                )
                or (
                    event["event_type"] == event_type
                    and event["expected_date"] == expected_date.isoformat()
                    and event["account_id"] == account_id
                    and event["amount"] == amount
                )
                for event in events
            )

        for receivable in snapshot.receivables:
            if receivable.status.value in {"RECEIVED", "CANCELLED"}:
                continue
            expected_date = max(snapshot.as_of.date(), receivable.expected_date)
            if already_present(
                source_reference_id=receivable.receivable_id,
                event_type="RECEIVABLE",
                expected_date=expected_date,
                account_id=receivable.destination_account_id,
                amount=receivable.amount,
            ):
                continue
            confirmed = receivable.status.value == "CONFIRMED" and receivable.user_confirmed
            events.append(
                {
                    "event_id": f"receivable:{receivable.receivable_id}",
                    "event_type": "RECEIVABLE",
                    "direction": "INFLOW",
                    "amount": receivable.amount,
                    "expected_date": expected_date.isoformat(),
                    "account_id": receivable.destination_account_id,
                    "counterparty_id": receivable.counterparty_id,
                    "certainty": "CONFIRMED" if confirmed else "ESTIMATED",
                    "is_essential": False,
                    "is_adjustable": False,
                    "source": receivable.source.value,
                    "source_reference_id": receivable.receivable_id,
                }
            )

        cards = {card.card_id: card for card in snapshot.cards}
        for card in snapshot.cards:
            if card.current_billing_amount <= 0 or already_present(
                source_reference_id=card.card_id,
                event_type="CARD_BILL",
                expected_date=card.billing_date,
                account_id=card.payment_account_id,
                amount=card.current_billing_amount,
            ):
                continue
            events.append(
                {
                    "event_id": f"card-bill:{card.card_id}",
                    "event_type": "CARD_BILL",
                    "direction": "OUTFLOW",
                    "amount": card.current_billing_amount,
                    "expected_date": card.billing_date.isoformat(),
                    "account_id": card.payment_account_id,
                    "counterparty_id": None,
                    "certainty": "CONFIRMED",
                    "is_essential": True,
                    "is_adjustable": False,
                    "source": "SYSTEM_DERIVED",
                    "source_reference_id": card.card_id,
                }
            )
        for plan in snapshot.installment_plans:
            if plan.status.value != "ACTIVE" or plan.remaining_months == 0:
                continue
            card = cards.get(plan.card_id)
            if card is None:
                raise ServiceError(
                    "INSUFFICIENT_DATA",
                    "할부에 연결된 카드 정보를 찾을 수 없습니다.",
                    details={"installment_plan_id": plan.installment_plan_id},
                    http_status=422,
                )
            for index in range(plan.remaining_months):
                payment_date = CoreToolService._add_months(plan.next_payment_date, index)
                if (
                    index == 0
                    and card.current_billing_amount > 0
                    and card.billing_date == payment_date
                ) or already_present(
                    source_reference_id=plan.installment_plan_id,
                    event_type="INSTALLMENT_PAYMENT",
                    expected_date=payment_date,
                    account_id=card.payment_account_id,
                    amount=plan.monthly_payment,
                ):
                    continue
                events.append(
                    {
                        "event_id": (f"installment:{plan.installment_plan_id}:{index + 1}"),
                        "event_type": "INSTALLMENT_PAYMENT",
                        "direction": "OUTFLOW",
                        "amount": plan.monthly_payment,
                        "expected_date": payment_date.isoformat(),
                        "account_id": card.payment_account_id,
                        "counterparty_id": None,
                        "certainty": "CONFIRMED",
                        "is_essential": True,
                        "is_adjustable": False,
                        "source": "SYSTEM_DERIVED",
                        "source_reference_id": plan.installment_plan_id,
                    }
                )
        return sorted(events, key=lambda item: (item["expected_date"], item["event_id"]))

    @staticmethod
    def _add_months(value: date, months: int) -> date:
        month_index = value.month - 1 + months
        year = value.year + month_index // 12
        month = month_index % 12 + 1
        day = min(value.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def calculate_safe_to_spend(
        self,
        snapshot_id: str,
        *,
        horizon_days: int = 91,
        protection_level: float | None = None,
        seed: int = 42,
    ) -> dict[str, Any]:
        if horizon_days != 91:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "MVP 분석기간은 91일로 고정되어 있습니다.",
                details={"horizon_days": horizon_days},
                http_status=422,
            )
        try:
            result = calculate_safe_to_spend(
                self._snapshot(snapshot_id),
                protection_level=protection_level,
                seed=seed,
            )
            return jsonable(result)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                "SAFE_TO_SPEND_CALCULATION_FAILED",
                "Safe-to-Spend 계산에 실패했습니다.",
                retryable=False,
                http_status=422,
            ) from exc

    def evaluate_action_plan(
        self,
        snapshot_id: str,
        *,
        actions: list[dict[str, Any]],
        seed: int = 42,
    ) -> dict[str, Any]:
        parsed_actions = self._actions(actions)
        try:
            return jsonable(
                evaluate_action_plan(self._snapshot(snapshot_id), parsed_actions, seed=seed)
            )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                "ACTION_NOT_FEASIBLE",
                "대응안을 가상 적용할 수 없습니다.",
                retryable=False,
                http_status=422,
            ) from exc

    def validate_financial_policy(
        self,
        snapshot_id: str,
        *,
        actions: list[dict[str, Any]],
        evaluation_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed_actions = self._actions(actions)
        evaluation: CashflowAnalysis | None = None
        if evaluation_result:
            try:
                evaluation = ActionEvaluation.model_validate(evaluation_result).after
            except ValidationError:
                try:
                    evaluation = CashflowAnalysis.model_validate(evaluation_result)
                except ValidationError as exc:
                    raise ServiceError(
                        "ACTION_SCHEMA_INVALID",
                        "대응안 평가 결과 형식이 올바르지 않습니다.",
                        details={"validation_errors": self._validation_errors(exc)},
                        http_status=422,
                    ) from exc
        try:
            return jsonable(
                validate_financial_policy(
                    self._snapshot(snapshot_id),
                    parsed_actions,
                    evaluation=evaluation,
                )
            )
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                "POLICY_VALIDATION_FAILED",
                "금융 안전정책 검증에 실패했습니다.",
                retryable=False,
                http_status=422,
            ) from exc

    def apply_actions_virtual(
        self,
        snapshot_id: str,
        *,
        actions: list[dict[str, Any]],
        new_snapshot_id: str,
    ) -> FinancialSnapshot:
        """Create an in-memory virtual snapshot; this never changes current records."""

        parsed_actions = self._actions(actions)
        try:
            applied = core_apply_actions_virtual(self._snapshot(snapshot_id), parsed_actions)
            return applied.model_copy(update={"snapshot_id": new_snapshot_id})
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError(
                "ACTION_NOT_FEASIBLE",
                "대응안을 새 가상 스냅숏에 적용할 수 없습니다.",
                retryable=False,
                http_status=422,
            ) from exc

    def _snapshot(self, snapshot_id: str) -> FinancialSnapshot:
        try:
            payload = self.repository.get_snapshot(snapshot_id)
        except RecordNotFound as exc:
            raise ServiceError(
                "SNAPSHOT_NOT_FOUND",
                "금융 스냅숏을 찾을 수 없습니다.",
                details={"snapshot_id": snapshot_id},
                http_status=404,
            ) from exc
        try:
            return FinancialSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise ServiceError(
                "INSUFFICIENT_DATA",
                "저장된 금융 스냅숏이 유효하지 않습니다.",
                details={"validation_errors": self._validation_errors(exc)},
                http_status=422,
            ) from exc

    @staticmethod
    def _actions(actions: list[dict[str, Any]]) -> list[Action]:
        try:
            return ACTION_LIST_ADAPTER.validate_python(actions)
        except ValidationError as exc:
            raise ServiceError(
                "ACTION_SCHEMA_INVALID",
                "대응안 행동 형식이 올바르지 않습니다.",
                details={"validation_errors": CoreToolService._validation_errors(exc)},
                http_status=422,
            ) from exc

    @staticmethod
    def _validation_errors(error: ValidationError) -> list[dict[str, Any]]:
        return [
            {
                "type": item["type"],
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
            }
            for item in error.errors()
        ]
