"""Current-state data ingestion and user-confirmation workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from flowguard.adapters import CSVFinancialDataAdapter, CSVImportError
from flowguard.domain import (
    Counterparty,
    InstallmentPlan,
    Receivable,
    ScheduledCashEvent,
    UserPreferences,
)
from flowguard.storage import (
    CURRENT_KINDS,
    FlowGuardRepository,
    RecordNotFound,
    StorageConflict,
    StorageError,
)

from .errors import ServiceError, not_found


class DataService:
    """Store validated financial inputs without promoting uncertain derivations."""

    def __init__(
        self,
        repository: FlowGuardRepository,
        *,
        csv_adapter: CSVFinancialDataAdapter | None = None,
    ) -> None:
        self.repository = repository
        self.csv_adapter = csv_adapter or CSVFinancialDataAdapter()

    def import_csv(self, user_id: str, content: bytes | str) -> dict[str, Any]:
        try:
            normalized = self.csv_adapter.parse(content)
        except CSVImportError as exc:
            raise ServiceError(
                exc.code,
                exc.message,
                details=exc.details,
                http_status=422,
            ) from exc

        notices: list[dict[str, Any]] = []
        for notice in normalized.data_quality_notices:
            notice_id = self._notice_id(notice)
            notices.append({**notice, "notice_id": notice_id})
        records = normalized.records()
        existing_candidates = {
            item["candidate_id"]: item
            for item in self.repository.list_records(user_id, "candidates")
        }
        for candidate in records["candidates"]:
            existing = existing_candidates.get(candidate["candidate_id"])
            if existing and existing.get("status") != "PENDING":
                candidate["status"] = existing["status"]
        counts = {kind: len(items) for kind, items in records.items()}
        self.repository.apply_record_bundle(
            user_id,
            {**records, "data_quality_notices": notices},
            replace_kinds={"candidates", "data_quality_notices"},
        )

        return {
            "format": normalized.format,
            "imported_count": sum(counts.values()),
            "imported_counts": counts,
            "candidates": normalized.candidates,
            "data_quality_notices": notices,
        }

    def list_records(self, user_id: str, kind: str) -> list[dict[str, Any]]:
        self._ensure_kind(kind)
        return self.repository.list_records(user_id, kind)

    def get_record(self, user_id: str, kind: str, record_id: str) -> dict[str, Any]:
        self._ensure_kind(kind)
        try:
            return self.repository.get_record(user_id, kind, record_id)
        except RecordNotFound as exc:
            raise not_found(kind, record_id) from exc

    def create_record(self, user_id: str, kind: str, record: Mapping[str, Any]) -> dict[str, Any]:
        self._ensure_kind(kind)
        id_field = CURRENT_KINDS[kind]
        record_id = record.get(id_field)
        if not isinstance(record_id, str) or not record_id:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                f"{id_field} 값이 필요합니다.",
                details={"field": id_field},
                http_status=422,
            )
        try:
            self.repository.get_record(user_id, kind, record_id)
        except RecordNotFound:
            pass
        else:
            raise ServiceError(
                "DUPLICATE_FINANCIAL_EVENT",
                "같은 식별자의 데이터가 이미 존재합니다.",
                details={"kind": kind, "record_id": record_id},
                http_status=409,
            )
        try:
            self.repository.upsert_records(user_id, kind, [record])
        except StorageError as exc:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "금융 데이터를 저장할 수 없습니다.",
                details={"kind": kind},
                http_status=422,
            ) from exc
        return self.repository.get_record(user_id, kind, record_id)

    def patch_record(
        self,
        user_id: str,
        kind: str,
        record_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._ensure_kind(kind)
        if not changes:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "수정할 필드가 없습니다.",
                http_status=422,
            )
        try:
            return self.repository.patch_record(user_id, kind, record_id, changes)
        except RecordNotFound as exc:
            raise not_found(kind, record_id) from exc
        except StorageConflict as exc:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "식별자는 변경할 수 없습니다.",
                details={"kind": kind, "record_id": record_id},
                http_status=409,
            ) from exc

    def decide_candidate(
        self,
        user_id: str,
        candidate_id: str,
        *,
        decision: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        result, bundle = self._prepare_candidate_decision(
            user_id,
            candidate_id,
            decision=decision,
            details=details,
        )
        self.repository.apply_record_bundle(user_id, bundle)
        return result

    def commit_setup(
        self,
        user_id: str,
        *,
        preferences: Mapping[str, Any],
        candidates: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Validate and persist all setup choices in one transaction."""

        prepared_preferences = self._prepare_preferences(user_id, preferences)
        prepared: dict[str, dict[str, dict[str, Any]]] = {
            "preferences": {"preferences": prepared_preferences}
        }
        results: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()

        for item in candidates:
            candidate_id = str(item["candidate_id"])
            if candidate_id in seen_candidate_ids:
                raise ServiceError(
                    "INVALID_FINANCIAL_EVENT",
                    "같은 후보를 한 번만 확정할 수 있습니다.",
                    details={"candidate_id": candidate_id},
                    http_status=422,
                )
            seen_candidate_ids.add(candidate_id)
            result, bundle = self._prepare_candidate_decision(
                user_id,
                candidate_id,
                decision=str(item["decision"]),
                details=item.get("details"),
            )
            results.append(result)
            for kind, records in bundle.items():
                id_field = CURRENT_KINDS[kind]
                target = prepared.setdefault(kind, {})
                for record in records:
                    target[str(record[id_field])] = record

        self.repository.apply_record_bundle(
            user_id,
            {kind: list(records.values()) for kind, records in prepared.items()},
        )
        return {
            "preferences": prepared_preferences,
            "candidates": results,
        }

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        preferences = self.repository.list_records(user_id, "preferences")
        if preferences:
            return preferences[0]
        return {
            "preference_id": "preferences",
            "protection_level": 0.9,
            "minimum_total_reserve": 0,
            "income_type": None,
        }

    def update_preferences(self, user_id: str, changes: Mapping[str, Any]) -> dict[str, Any]:
        updated = self._prepare_preferences(user_id, changes)
        self.repository.upsert_records(user_id, "preferences", [updated])
        return self.get_preferences(user_id)

    def _prepare_preferences(
        self,
        user_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        unknown = set(changes) - {
            "protection_level",
            "minimum_total_reserve",
            "income_type",
        }
        if unknown:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "지원하지 않는 사용자 설정입니다.",
                details={"fields": sorted(unknown)},
                http_status=422,
            )
        current = self.get_preferences(user_id)
        updated = {**current, **dict(changes), "preference_id": "preferences"}
        try:
            UserPreferences.model_validate(
                {
                    "protection_level": updated["protection_level"],
                    "minimum_total_reserve": updated["minimum_total_reserve"],
                }
            )
        except ValidationError as exc:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "사용자 보호 설정이 올바르지 않습니다.",
                details={"validation_errors": self._validation_errors(exc)},
                http_status=422,
            ) from exc
        return updated

    def _prepare_candidate_decision(
        self,
        user_id: str,
        candidate_id: str,
        *,
        decision: str,
        details: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        normalized_decision = decision.upper()
        if normalized_decision not in {"CONFIRMED", "REJECTED", "UNKNOWN"}:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "후보 확인값은 CONFIRMED, REJECTED, UNKNOWN 중 하나여야 합니다.",
                details={"decision": decision},
                http_status=422,
            )
        candidate = self.get_record(user_id, "candidates", candidate_id)
        decided = {**candidate, "status": normalized_decision}
        if normalized_decision != "CONFIRMED":
            return decided, {"candidates": [decided]}

        try:
            promotion_kind, promoted, additional = self._promote_candidate(
                user_id, candidate, details or {}
            )
        except ValidationError as exc:
            raise ServiceError(
                "CANDIDATE_CONFIRMATION_NEEDS_DETAILS",
                "후보 확정 정보가 올바르지 않습니다.",
                details={"validation_errors": self._validation_errors(exc)},
                http_status=422,
            ) from exc
        bundle = {
            "candidates": [decided],
            promotion_kind: [promoted],
            **additional,
        }
        return {
            **decided,
            "promotion": {"kind": promotion_kind, "record": promoted},
        }, bundle

    def _promote_candidate(
        self,
        user_id: str,
        candidate: Mapping[str, Any],
        details: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], dict[str, list[dict[str, Any]]]]:
        candidate_type = candidate.get("candidate_type")
        proposed = {**candidate.get("proposed_record", {}), **dict(details)}
        now = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
        candidate_id = str(candidate["candidate_id"])
        if candidate_type == "RECURRING_INCOME":
            required = {
                "amount",
                "expected_date",
                "destination_account_id",
            }
            self._require_promotion_details(candidate_id, proposed, required)
            counterparty_id, additional = self._resolve_candidate_counterparty(
                user_id,
                candidate_id,
                proposed,
                now,
                counterparty_type="CLIENT",
                required=True,
            )
            self.get_record(user_id, "accounts", str(proposed["destination_account_id"]))
            model = Receivable.model_validate(
                {
                    "receivable_id": proposed.get("receivable_id", f"receivable-{candidate_id}"),
                    "counterparty_id": counterparty_id,
                    "amount": proposed["amount"],
                    "expected_date": proposed["expected_date"],
                    "status": proposed.get("status", "ESTIMATED"),
                    "destination_account_id": proposed["destination_account_id"],
                    "user_confirmed": True,
                    "source": "USER_CONFIRMED",
                    "updated_at": now,
                }
            )
            return "receivables", model.model_dump(mode="json"), additional
        if candidate_type == "FIXED_EXPENSE":
            required = {
                "amount",
                "expected_date",
                "account_id",
                "event_type",
                "is_essential",
            }
            self._require_promotion_details(candidate_id, proposed, required)
            self.get_record(user_id, "accounts", str(proposed["account_id"]))
            counterparty_id, additional = self._resolve_candidate_counterparty(
                user_id,
                candidate_id,
                proposed,
                now,
                counterparty_type="MERCHANT",
                required=False,
            )
            model = ScheduledCashEvent.model_validate(
                {
                    "event_id": proposed.get("event_id", f"event-{candidate_id}"),
                    "event_type": proposed["event_type"],
                    "direction": "OUTFLOW",
                    "amount": proposed["amount"],
                    "expected_date": proposed["expected_date"],
                    "account_id": proposed["account_id"],
                    "counterparty_id": counterparty_id,
                    "certainty": "CONFIRMED",
                    "is_essential": proposed["is_essential"],
                    "is_adjustable": proposed.get("is_adjustable", False),
                    "source": "USER_CONFIRMED",
                    "source_reference_id": candidate_id,
                }
            )
            return "scheduled_events", model.model_dump(mode="json"), additional
        if candidate_type == "INSTALLMENT":
            required = {
                "card_id",
                "original_amount",
                "total_months",
                "next_payment_date",
            }
            self._require_promotion_details(candidate_id, proposed, required)
            self.get_record(user_id, "cards", str(proposed["card_id"]))
            months = int(proposed["total_months"])
            amount = int(proposed["original_amount"])
            model = InstallmentPlan.model_validate(
                {
                    "installment_plan_id": proposed.get(
                        "installment_plan_id", f"installment-{candidate_id}"
                    ),
                    "card_id": proposed["card_id"],
                    "original_amount": amount,
                    "monthly_payment": proposed.get(
                        "monthly_payment", (amount + months - 1) // months
                    ),
                    "total_months": months,
                    "remaining_months": proposed.get("remaining_months", months),
                    "next_payment_date": proposed["next_payment_date"],
                    "status": "ACTIVE",
                }
            )
            return "installment_plans", model.model_dump(mode="json"), {}
        raise ServiceError(
            "INVALID_FINANCIAL_EVENT",
            "지원하지 않는 자동 탐지 후보입니다.",
            details={"candidate_id": candidate_id, "candidate_type": candidate_type},
            http_status=422,
        )

    def _resolve_candidate_counterparty(
        self,
        user_id: str,
        candidate_id: str,
        proposed: Mapping[str, Any],
        now: str,
        *,
        counterparty_type: str,
        required: bool,
    ) -> tuple[str | None, dict[str, list[dict[str, Any]]]]:
        raw_name = proposed.get("counterparty_name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        raw_id = proposed.get("counterparty_id")
        counterparty_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None

        if name:
            matching = [
                item
                for item in self.repository.list_records(user_id, "counterparties")
                if str(item.get("name", "")).strip().casefold() == name.casefold()
            ]
            if len(matching) > 1:
                matching_by_id = {item["counterparty_id"]: item for item in matching}
                if counterparty_id not in matching_by_id:
                    raise ServiceError(
                        "CANDIDATE_CONFIRMATION_NEEDS_DETAILS",
                        "같은 이름의 거래처가 여러 개여서 거래처 선택이 필요합니다.",
                        details={
                            "candidate_id": candidate_id,
                            "required_fields": ["counterparty_id"],
                            "matching_counterparty_ids": sorted(matching_by_id),
                        },
                        http_status=422,
                    )
                return counterparty_id, {}
            if matching:
                return str(matching[0]["counterparty_id"]), {}

        if counterparty_id:
            try:
                self.get_record(user_id, "counterparties", counterparty_id)
            except ServiceError as exc:
                if exc.http_status != 404:
                    raise
            else:
                return counterparty_id, {}

        if not name:
            if required:
                raise ServiceError(
                    "CANDIDATE_CONFIRMATION_NEEDS_DETAILS",
                    "후보를 금융 데이터로 확정하려면 거래처 정보가 필요합니다.",
                    details={
                        "candidate_id": candidate_id,
                        "required_fields": ["counterparty_name"],
                    },
                    http_status=422,
                )
            return None, {}

        if counterparty_id is None:
            digest = hashlib.sha256(name.casefold().encode()).hexdigest()[:16]
            counterparty_id = f"counterparty-{digest}"
        counterparty = Counterparty.model_validate(
            {
                "counterparty_id": counterparty_id,
                "name": name,
                "counterparty_type": counterparty_type,
                "is_recurring": True,
                "payment_history_count": 0,
                "updated_at": now,
            }
        )
        return counterparty_id, {"counterparties": [counterparty.model_dump(mode="json")]}

    @staticmethod
    def _require_promotion_details(
        candidate_id: str,
        proposed: Mapping[str, Any],
        required: set[str],
    ) -> None:
        missing = sorted(name for name in required if proposed.get(name) is None)
        if missing:
            raise ServiceError(
                "CANDIDATE_CONFIRMATION_NEEDS_DETAILS",
                "후보를 금융 데이터로 확정하려면 추가 정보가 필요합니다.",
                details={
                    "candidate_id": candidate_id,
                    "required_fields": missing,
                },
                http_status=422,
            )

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

    @staticmethod
    def _ensure_kind(kind: str) -> None:
        if kind not in CURRENT_KINDS:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "지원하지 않는 데이터 종류입니다.",
                details={"kind": kind},
                http_status=422,
            )

    @staticmethod
    def _notice_id(notice: Mapping[str, Any]) -> str:
        identity = ":".join(
            [
                str(notice.get("code", "")),
                *(str(item) for item in notice.get("record_ids", [])),
            ]
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
        return f"notice-{digest}"
