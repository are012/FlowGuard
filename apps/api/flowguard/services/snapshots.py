"""Build immutable, validated snapshots from mutable current-state records."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from flowguard.domain import FinancialSnapshot
from flowguard.storage import FlowGuardRepository

from .errors import ServiceError

UPDATED_AT_KINDS = {
    "accounts": "account_id",
    "cards": "card_id",
    "counterparties": "counterparty_id",
    "receivables": "receivable_id",
}


class SnapshotBuilder:
    """Freeze current data while making every derived field visible in quality metadata."""

    def __init__(self, repository: FlowGuardRepository) -> None:
        self.repository = repository

    def build(
        self,
        user_id: str,
        *,
        as_of: datetime | None = None,
        snapshot_id: str | None = None,
    ) -> FinancialSnapshot:
        return self.build_with_revision(user_id, as_of=as_of, snapshot_id=snapshot_id)[0]

    def build_with_revision(
        self,
        user_id: str,
        *,
        as_of: datetime | None = None,
        snapshot_id: str | None = None,
    ) -> tuple[FinancialSnapshot, str]:
        effective_as_of = as_of or datetime.now(ZoneInfo("Asia/Seoul"))
        if effective_as_of.tzinfo is None:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "분석 기준시각에는 시간대가 필요합니다.",
                details={"field": "as_of"},
                http_status=422,
            )
        effective_as_of = effective_as_of.astimezone(ZoneInfo("Asia/Seoul"))
        snapshot_id = snapshot_id or f"snapshot-{uuid4()}"
        records, revision = self.repository.current_records_with_revision(user_id)
        body = self._snapshot_body(user_id, effective_as_of, snapshot_id, records)
        try:
            snapshot = FinancialSnapshot.model_validate(body)
        except ValidationError as exc:
            raise ServiceError(
                "INSUFFICIENT_DATA",
                "유효한 금융 스냅숏을 만들 수 없습니다.",
                details={"validation_errors": self._safe_validation_errors(exc)},
                http_status=422,
            ) from exc

        self.repository.create_snapshot(
            user_id,
            as_of=effective_as_of,
            snapshot_id=snapshot_id,
            payload=snapshot.model_dump(mode="json"),
        )
        return snapshot, revision

    def _snapshot_body(
        self,
        user_id: str,
        as_of: datetime,
        snapshot_id: str,
        records: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        collection_names = (
            "accounts",
            "cards",
            "transactions",
            "counterparties",
            "payment_histories",
            "scheduled_events",
            "receivables",
            "installment_plans",
            "protected_funds",
        )
        body = {name: deepcopy(records[name]) for name in collection_names}
        candidates = records["candidates"]
        notices = records["data_quality_notices"]
        stored_preferences = records["preferences"]
        preferences = stored_preferences[0] if stored_preferences else {}

        derived_timestamps: list[str] = []
        for kind, id_field in UPDATED_AT_KINDS.items():
            for record in body[kind]:
                record["updated_at"] = as_of.isoformat()
                derived_timestamps.append(f"{kind}:{record[id_field]}")

        # ``installment_months`` exists only as an import-time detection signal.
        # A confirmed installment has its own InstallmentPlan record.
        for transaction in body["transactions"]:
            transaction.pop("installment_months", None)

        missing_sources: list[str] = []
        account_ids = {item["account_id"] for item in body["accounts"]}
        for notice in notices:
            if notice.get("severity") != "ERROR":
                continue
            record_ids = [str(item) for item in notice.get("record_ids", ())]
            if notice.get("code") == "ACCOUNT_DETAILS_REQUIRED":
                record_ids = [item for item in record_ids if item not in account_ids]
            missing_sources.extend(record_ids)
        unconfirmed_items = [
            item["candidate_id"]
            for item in candidates
            if item.get("status") in {"PENDING", "UNKNOWN"}
        ]
        unconfirmed_items.extend(f"derived_updated_at:{item}" for item in derived_timestamps)
        known_counterparties = {item["counterparty_id"] for item in body["counterparties"]}
        counterparties_with_history = {
            item["counterparty_id"] for item in body["payment_histories"]
        }
        for receivable in body["receivables"]:
            counterparty_id = receivable["counterparty_id"]
            if receivable.get("status") not in {"RECEIVED", "CANCELLED"} and (
                counterparty_id not in known_counterparties
                or counterparty_id not in counterparties_with_history
            ):
                unconfirmed_items.append(f"counterparty_evidence:{counterparty_id}")

        return {
            "snapshot_id": snapshot_id,
            "user_id": user_id,
            "as_of": as_of.isoformat(),
            "timezone": "Asia/Seoul",
            **body,
            "preferences": {
                "protection_level": preferences.get("protection_level", 0.9),
                "minimum_total_reserve": preferences.get("minimum_total_reserve", 0),
            },
            "data_quality": {
                "missing_sources": sorted(set(missing_sources)),
                "stale_sources": [],
                "unconfirmed_items": sorted(set(unconfirmed_items)),
            },
        }

    @staticmethod
    def _safe_validation_errors(error: ValidationError) -> list[dict[str, Any]]:
        return [
            {
                "type": item["type"],
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
            }
            for item in error.errors()
        ]
