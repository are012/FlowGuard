"""Current-state data ingestion and user-confirmation workflows."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from flowguard.adapters import (
    CSVFinancialDataAdapter,
    CSVImportError,
    CSVImportResult,
    TransactionGroup,
)
from flowguard.config import (
    AI_CLASSIFICATION_CONTRACT_VERSION,
    AI_CLASSIFICATION_PROMPT_VERSION,
    AI_CLASSIFICATION_SCHEMA_VERSION,
    AI_DEFAULT_LOCALE,
)
from flowguard.domain import (
    Counterparty,
    InstallmentPlan,
    Receivable,
    ScheduledCashEvent,
    UserPreferences,
)
from flowguard.storage import (
    CURRENT_KINDS,
    DataRevisionConflict,
    FlowGuardRepository,
    RecordNotFound,
    StorageConflict,
    StorageError,
)

from .errors import ServiceError, not_found
from .import_classification import (
    LabelInventory,
    build_label_inventory,
    classification_import_id,
    classification_summary,
    project_validated_groups,
)
from .label_group_validation import LabelClassificationOutcome

ClassificationMode = Literal["off", "shadow", "on"]


class LabelClassifier(Protocol):
    def classify(self, payload: Mapping[str, Any]) -> LabelClassificationOutcome: ...


class DataService:
    """Store validated financial inputs without promoting uncertain derivations."""

    def __init__(
        self,
        repository: FlowGuardRepository,
        *,
        csv_adapter: CSVFinancialDataAdapter | None = None,
        classification_mode: ClassificationMode = "off",
        classification_client: LabelClassifier | None = None,
        classification_locale: str = AI_DEFAULT_LOCALE,
        classification_model_name: str | None = None,
    ) -> None:
        self.repository = repository
        self.csv_adapter = csv_adapter or CSVFinancialDataAdapter()
        self.classification_mode = classification_mode
        self.classification_client = classification_client
        self.classification_locale = classification_locale
        self.classification_model_name = classification_model_name

    def import_csv(self, user_id: str, content: bytes | str) -> dict[str, Any]:
        classification_result: dict[str, Any] | None = None
        classification_completion: dict[str, Any] | None = None
        initial_revision = (
            self.repository.current_state_revision(user_id)
            if self.classification_mode != "off"
            else None
        )

        def classify_transactions(
            transactions: list[dict[str, Any]],
        ) -> list[TransactionGroup] | None:
            nonlocal classification_completion, classification_result
            (
                groups,
                classification_result,
                classification_completion,
            ) = self._classify_transaction_labels(
                user_id,
                transactions,
            )
            return groups

        try:
            normalized = self.csv_adapter.parse(
                content,
                group_transactions=(
                    classify_transactions if self.classification_mode != "off" else None
                ),
            )
        except CSVImportError as exc:
            raise ServiceError(
                exc.code,
                exc.message,
                details=exc.details,
                http_status=422,
            ) from exc

        try:
            records, notices, counts, user_split_group_count = self._persist_import_records(
                user_id,
                normalized,
                classification_completion=classification_completion,
                initial_revision=initial_revision,
            )
        except DataRevisionConflict as exc:
            self._mark_classification_import_failed(
                classification_completion,
                error_code="import_state_changed",
            )
            raise ServiceError(
                "IMPORT_STATE_CHANGED",
                "가져오는 동안 후보 상태가 변경되어 데이터를 저장하지 않았습니다.",
                retryable=True,
                http_status=409,
            ) from exc
        except RecordNotFound as exc:
            raise ServiceError(
                "IMPORT_STATE_CHANGED",
                "가져오는 동안 분류 감사 기록이 초기화되어 데이터를 저장하지 않았습니다.",
                retryable=True,
                http_status=409,
            ) from exc
        except StorageError as storage_error:
            if classification_completion is None:
                raise
            recovered = (
                self._recover_succeeded_classification(
                    user_id,
                    content,
                    classification_completion,
                )
                if isinstance(storage_error, StorageConflict)
                else None
            )
            if recovered is not None:
                normalized, classification_result, recovered_completion = recovered
                try:
                    records, notices, counts, user_split_group_count = self._persist_import_records(
                        user_id,
                        normalized,
                        classification_completion=recovered_completion,
                        initial_revision=initial_revision,
                    )
                except DataRevisionConflict as exc:
                    raise ServiceError(
                        "IMPORT_STATE_CHANGED",
                        "가져오는 동안 후보 상태가 변경되어 데이터를 저장하지 않았습니다.",
                        retryable=True,
                        http_status=409,
                    ) from exc
                except RecordNotFound as exc:
                    raise ServiceError(
                        "IMPORT_STATE_CHANGED",
                        "가져오는 동안 분류 감사 기록이 초기화되어 데이터를 저장하지 않았습니다.",
                        retryable=True,
                        http_status=409,
                    ) from exc
                except StorageError:
                    # Once an applied terminal audit exists, never overwrite its
                    # AI-derived candidates with a deterministic retry.
                    raise
            else:
                self._mark_classification_import_failed(
                    classification_completion,
                    error_code="import_persistence_failed",
                )
                try:
                    normalized = self.csv_adapter.parse(content)
                except CSVImportError as exc:
                    raise ServiceError(
                        exc.code,
                        exc.message,
                        details=exc.details,
                        http_status=422,
                    ) from exc
                try:
                    records, notices, counts, user_split_group_count = self._persist_import_records(
                        user_id,
                        normalized,
                        initial_revision=initial_revision,
                    )
                except DataRevisionConflict as exc:
                    raise ServiceError(
                        "IMPORT_STATE_CHANGED",
                        "가져오는 동안 후보 상태가 변경되어 데이터를 저장하지 않았습니다.",
                        retryable=True,
                        http_status=409,
                    ) from exc
                original_label_count = int(
                    (classification_result or {})
                    .get("classification_summary", {})
                    .get("original_label_count", 0)
                )
                classification_result = {
                    "import_id": (classification_result or {}).get("import_id"),
                    "classification_status": "FAILED",
                    "classification_summary": {
                        "source": "DETERMINISTIC_FALLBACK",
                        "applied": False,
                        "original_label_count": original_label_count,
                        "grouped_entity_count": original_label_count,
                        "merged_label_count": 0,
                    },
                }

        response = {
            "format": normalized.format,
            "imported_count": sum(counts.values()),
            "imported_counts": counts,
            "candidates": normalized.candidates,
            "data_quality_notices": notices,
        }
        if self.classification_mode != "off":
            response.update(
                classification_result
                or {
                    "classification_status": "NOT_REQUESTED",
                    "classification_summary": {
                        "source": "DETERMINISTIC_FALLBACK",
                        "applied": False,
                        "original_label_count": 0,
                        "grouped_entity_count": 0,
                        "merged_label_count": 0,
                    },
                }
            )
            if user_split_group_count:
                response["classification_summary"] = {
                    **response["classification_summary"],
                    "user_split_group_count": user_split_group_count,
                }
        return response

    def _persist_import_records(
        self,
        user_id: str,
        normalized: CSVImportResult,
        *,
        classification_completion: Mapping[str, Any] | None = None,
        initial_revision: str | None = None,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        list[dict[str, Any]],
        dict[str, int],
        int,
    ]:
        """Reconcile candidate state again if a classified import loses a revision race."""

        attempts = 2 if self.classification_mode != "off" else 1
        pristine = deepcopy(normalized)
        for attempt in range(attempts):
            attempt_normalized = deepcopy(pristine)
            expected_revision = (
                initial_revision
                if attempt == 0
                else self.repository.current_state_revision(user_id)
                if self.classification_mode != "off"
                else None
            )
            prepared = self._prepare_import_records(user_id, attempt_normalized)
            records, notices, _, _ = prepared
            kwargs: dict[str, Any] = {
                "replace_kinds": {"candidates", "data_quality_notices"},
                "classification_completion": classification_completion,
            }
            if expected_revision is not None:
                kwargs["expected_revision"] = expected_revision
            try:
                self.repository.apply_record_bundle(
                    user_id,
                    {**records, "data_quality_notices": notices},
                    **kwargs,
                )
            except DataRevisionConflict:
                if attempt + 1 == attempts:
                    raise
                if classification_completion is None:
                    raise
                self._assert_classification_guard_live(
                    user_id,
                    classification_completion,
                )
                continue
            normalized.candidates = deepcopy(attempt_normalized.candidates)
            normalized.data_quality_notices = deepcopy(attempt_normalized.data_quality_notices)
            return prepared
        raise AssertionError("import persistence attempts were exhausted")

    def _assert_classification_guard_live(
        self,
        user_id: str,
        guard: Mapping[str, Any],
    ) -> None:
        run = self.repository.get_classification_run(str(guard["classification_id"]))
        expected_status = str(guard["status"])
        allowed_statuses = (
            {"IN_PROGRESS", "SUCCEEDED"}
            if expected_status == "SUCCEEDED" and guard.get("applied") is True
            else {expected_status}
        )
        if (
            run.get("user_id") != user_id
            or run.get("import_id") != guard.get("import_id")
            or run.get("label_set_hash") != guard.get("label_set_hash")
            or run.get("status") not in allowed_statuses
            or (
                run.get("status") == expected_status
                and run.get("applied") is not guard.get("applied")
            )
        ):
            raise DataRevisionConflict("classification audit changed during import retry")

    def _mark_classification_import_failed(
        self,
        classification_completion: Mapping[str, Any] | None,
        *,
        error_code: str,
    ) -> None:
        if classification_completion is None:
            return
        with suppress(StorageError):
            self.repository.update_classification_run(
                str(classification_completion["classification_id"]),
                status="FAILED",
                applied=False,
                attempt_count=int(classification_completion["attempt_count"]),
                fallback_used=True,
                latency_ms=int(classification_completion["latency_ms"]),
                error_code=error_code,
            )

    def _prepare_import_records(
        self,
        user_id: str,
        normalized: CSVImportResult,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        list[dict[str, Any]],
        dict[str, int],
        int,
    ]:
        records = normalized.records()
        existing_candidates = {
            item["candidate_id"]: item
            for item in self.repository.list_records(user_id, "candidates")
        }
        for candidate in records["candidates"]:
            existing = existing_candidates.get(candidate["candidate_id"])
            if existing and existing.get("status") != "PENDING":
                candidate["status"] = existing["status"]
        user_split_group_count = 0
        if self.classification_mode != "off":
            (
                records["candidates"],
                normalized.candidates,
                normalized.data_quality_notices,
                user_split_group_count,
            ) = self._restore_user_split_candidates(
                transactions=records["transactions"],
                candidates=records["candidates"],
                notices=normalized.data_quality_notices,
                existing_candidates=existing_candidates,
            )
        notices = [
            {**notice, "notice_id": self._notice_id(notice)}
            for notice in normalized.data_quality_notices
        ]
        counts = {kind: len(items) for kind, items in records.items()}
        return records, notices, counts, user_split_group_count

    def _restore_user_split_candidates(
        self,
        *,
        transactions: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        notices: list[dict[str, Any]],
        existing_candidates: Mapping[str, dict[str, Any]],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        int,
    ]:
        """Keep a user's AI-group split stable across later classified imports."""

        split_markers = {
            candidate_id: candidate
            for candidate_id, candidate in existing_candidates.items()
            if self._is_split_marker(candidate)
        }
        if not split_markers:
            return candidates, candidates, notices, 0

        transactions_by_id = {item["transaction_id"]: item for item in transactions}
        persisted: dict[str, dict[str, Any]] = {}
        visible: dict[str, dict[str, Any]] = {}
        restored_ids: set[str] = set()
        consumed_marker_ids: set[str] = set()
        for candidate in candidates:
            candidate_id = str(candidate["candidate_id"])
            marker = split_markers.get(candidate_id)
            if marker is None:
                marker_match = next(
                    (
                        (marker_id, item)
                        for marker_id, item in split_markers.items()
                        if self._split_marker_matches(item, candidate)
                    ),
                    None,
                )
                if marker_match is not None:
                    marker_id, marker = marker_match
                    consumed_marker_ids.add(marker_id)
            if marker is None:
                persisted[candidate_id] = candidate
                visible[candidate_id] = candidate
                continue

            restored_ids.add(candidate_id)
            classification = candidate.get("classification_group")
            if not isinstance(classification, Mapping):
                classification = marker.get("classification_group", {})
            superseded = {
                **candidate,
                "status": "REJECTED",
                "classification_group": {
                    **dict(classification),
                    "can_split": False,
                    "user_split": True,
                },
            }
            persisted[candidate_id] = superseded
            evidence = [
                transactions_by_id[transaction_id]
                for transaction_id in candidate.get("evidence_transaction_ids", [])
                if transaction_id in transactions_by_id
            ]
            alternatives = [
                item
                for item in self.csv_adapter.detect_candidates(evidence)
                if item.get("candidate_type") != "INSTALLMENT"
            ]
            for alternative in alternatives:
                alternative_id = str(alternative["candidate_id"])
                existing = existing_candidates.get(alternative_id)
                if existing is not None and existing.get("status") != "PENDING":
                    alternative["status"] = existing["status"]
                persisted[alternative_id] = alternative
                visible[alternative_id] = alternative

        for marker_id, marker in split_markers.items():
            if marker_id not in consumed_marker_ids:
                persisted.setdefault(marker_id, marker)

        base_notices = [
            notice for notice in notices if notice.get("code") != "USER_CONFIRMATION_REQUIRED"
        ]
        for candidate in visible.values():
            if candidate.get("status") != "PENDING":
                continue
            base_notices.append(
                {
                    "code": "USER_CONFIRMATION_REQUIRED",
                    "severity": "INFO",
                    "message": (
                        "자동 탐지 항목은 사용자가 확인하기 전까지 현금흐름에 반영되지 않습니다."
                    ),
                    "record_ids": [candidate["candidate_id"]],
                }
            )
        return (
            list(persisted.values()),
            list(visible.values()),
            base_notices,
            len(restored_ids),
        )

    @staticmethod
    def _is_split_marker(candidate: Mapping[str, Any]) -> bool:
        classification = candidate.get("classification_group")
        return isinstance(classification, Mapping) and classification.get("user_split") is True

    @staticmethod
    def _split_marker_matches(
        marker: Mapping[str, Any],
        candidate: Mapping[str, Any],
    ) -> bool:
        marker_classification = marker.get("classification_group")
        candidate_classification = candidate.get("classification_group")
        if not isinstance(marker_classification, Mapping) or not isinstance(
            candidate_classification, Mapping
        ):
            return False
        marker_labels = {
            str(label).strip().casefold()
            for label in marker_classification.get("labels", [])
            if str(label).strip()
        }
        candidate_labels = {
            str(label).strip().casefold()
            for label in candidate_classification.get("labels", [])
            if str(label).strip()
        }
        marker_direction = marker.get("proposed_record", {}).get("direction")
        candidate_direction = candidate.get("proposed_record", {}).get("direction")
        return (
            bool(marker_labels)
            and marker_labels <= candidate_labels
            and marker_direction == candidate_direction
        )

    def _classify_transaction_labels(
        self,
        user_id: str,
        transactions: list[dict[str, Any]],
    ) -> tuple[
        list[TransactionGroup] | None,
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        inventory = build_label_inventory(transactions)
        import_id = classification_import_id(
            user_id=user_id,
            mode=self.classification_mode,
            label_set_hash=inventory.label_set_hash,
            locale=self.classification_locale,
        )
        fallback_summary = self._fallback_classification_summary(inventory)
        if not inventory.items or self.classification_client is None:
            return (
                None,
                {
                    "import_id": import_id,
                    "classification_status": "NOT_REQUESTED",
                    "classification_summary": fallback_summary,
                },
                None,
            )

        latest_run = self.repository.latest_classification_run(
            user_id,
            import_id=import_id,
        )
        if latest_run is not None and latest_run["status"] == "SUCCEEDED":
            return self._reuse_classification_run(inventory, import_id, latest_run)

        if latest_run is not None and latest_run["status"] == "IN_PROGRESS":
            payload = inventory.request_payload(
                import_id=import_id,
                locale=self.classification_locale,
                request_id=str(latest_run["request_id"]),
                idempotency_key=str(latest_run["idempotency_key"]),
            )
        else:
            payload = inventory.request_payload(
                import_id=import_id,
                locale=self.classification_locale,
                retry_after=(
                    str(latest_run["classification_id"]) if latest_run is not None else None
                ),
            )
        try:
            run, created = self.repository.create_or_get_classification_run(
                user_id=user_id,
                import_id=import_id,
                request_id=str(payload["requestId"]),
                idempotency_key=str(payload["idempotencyKey"]),
                label_set_hash=inventory.label_set_hash,
                schema_version=AI_CLASSIFICATION_SCHEMA_VERSION,
                contract_version=AI_CLASSIFICATION_CONTRACT_VERSION,
                prompt_version=AI_CLASSIFICATION_PROMPT_VERSION,
                mode=self.classification_mode.upper(),
                label_count=len(inventory.items),
                model_name=self.classification_model_name,
            )
        except StorageError:
            return (
                None,
                {
                    "import_id": import_id,
                    "classification_status": "FAILED",
                    "classification_summary": fallback_summary,
                },
                None,
            )

        if not created and run["status"] in {"SUCCEEDED", "REJECTED", "FAILED"}:
            return self._reuse_classification_run(inventory, import_id, run)

        try:
            outcome = self.classification_client.classify(payload)
        except Exception:
            outcome = LabelClassificationOutcome(
                status="FAILED",
                response_payload={},
                attempt_count=0,
                latency_ms=0,
                error_code="client_exception",
            )

        applied = outcome.status == "SUCCEEDED" and self.classification_mode == "on"
        summary = (
            classification_summary(inventory, outcome.response_payload, applied=applied)
            if outcome.status == "SUCCEEDED"
            else fallback_summary
        )
        completion = None
        if applied:
            completion = {
                "classification_id": str(run["classification_id"]),
                "import_id": import_id,
                "label_set_hash": inventory.label_set_hash,
                "status": "SUCCEEDED",
                "applied": True,
                "attempt_count": outcome.attempt_count,
                "fallback_used": False,
                "latency_ms": outcome.latency_ms,
                "group_count": summary["grouped_entity_count"],
                "merged_label_count": summary["merged_label_count"],
                "response_payload": outcome.response_payload,
                "error_code": None,
            }
        else:
            try:
                completed_run = self.repository.update_classification_run(
                    str(run["classification_id"]),
                    status=outcome.status,
                    applied=False,
                    attempt_count=outcome.attempt_count,
                    fallback_used=True,
                    latency_ms=outcome.latency_ms,
                    group_count=(
                        summary["grouped_entity_count"] if outcome.status == "SUCCEEDED" else None
                    ),
                    merged_label_count=(
                        summary["merged_label_count"] if outcome.status == "SUCCEEDED" else None
                    ),
                    response_payload=(
                        outcome.response_payload if outcome.status == "SUCCEEDED" else None
                    ),
                    error_code=outcome.error_code,
                )
                completion = self._classification_persistence_guard(
                    inventory,
                    import_id,
                    completed_run,
                )
            except StorageConflict:
                stored = self._stored_terminal_classification(
                    user_id=user_id,
                    import_id=import_id,
                    label_set_hash=inventory.label_set_hash,
                    classification_id=str(run["classification_id"]),
                )
                if stored is not None:
                    return self._reuse_classification_run(inventory, import_id, stored)
                return (
                    None,
                    {
                        "import_id": import_id,
                        "classification_status": "FAILED",
                        "classification_summary": fallback_summary,
                    },
                    None,
                )
            except StorageError:
                return (
                    None,
                    {
                        "import_id": import_id,
                        "classification_status": "FAILED",
                        "classification_summary": fallback_summary,
                    },
                    None,
                )

        groups = project_validated_groups(inventory, outcome.response_payload) if applied else None
        return (
            groups,
            {
                "import_id": import_id,
                "classification_status": outcome.status,
                "classification_summary": summary,
            },
            completion,
        )

    def _reuse_classification_run(
        self,
        inventory: LabelInventory,
        import_id: str,
        run: Mapping[str, Any],
    ) -> tuple[
        list[TransactionGroup] | None,
        dict[str, Any],
        dict[str, Any] | None,
    ]:
        status = str(run["status"])
        response_payload = run.get("response_payload")
        applied = status == "SUCCEEDED" and self.classification_mode == "on"
        if status == "SUCCEEDED" and isinstance(response_payload, dict):
            summary = classification_summary(inventory, response_payload, applied=applied)
            groups = project_validated_groups(inventory, response_payload) if applied else None
        else:
            summary = self._fallback_classification_summary(inventory)
            groups = None
        completion = (
            self._classification_persistence_guard(inventory, import_id, run)
            if status in {"SUCCEEDED", "REJECTED", "FAILED"}
            else None
        )
        if applied:
            required = {
                "attempt_count",
                "latency_ms",
                "group_count",
                "merged_label_count",
            }
            if run.get("applied") is not True or any(run.get(field) is None for field in required):
                raise StorageConflict("stored classification success is incomplete")
            completion = {
                "classification_id": str(run["classification_id"]),
                "import_id": import_id,
                "label_set_hash": inventory.label_set_hash,
                "status": "SUCCEEDED",
                "applied": True,
                "attempt_count": int(run["attempt_count"]),
                "fallback_used": False,
                "latency_ms": int(run["latency_ms"]),
                "group_count": int(run["group_count"]),
                "merged_label_count": int(run["merged_label_count"]),
                "response_payload": response_payload,
                "error_code": None,
            }
        return (
            groups,
            {
                "import_id": import_id,
                "classification_status": status,
                "classification_summary": summary,
            },
            completion,
        )

    @staticmethod
    def _classification_persistence_guard(
        inventory: LabelInventory,
        import_id: str,
        run: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "classification_id": str(run["classification_id"]),
            "import_id": import_id,
            "label_set_hash": inventory.label_set_hash,
            "status": str(run["status"]),
            "applied": run.get("applied") is True,
            "attempt_count": int(run.get("attempt_count") or 0),
            "latency_ms": int(run.get("latency_ms") or 0),
        }

    def _stored_terminal_classification(
        self,
        *,
        user_id: str,
        import_id: str,
        label_set_hash: str,
        classification_id: str,
    ) -> dict[str, Any] | None:
        try:
            run = self.repository.get_classification_run(classification_id)
        except StorageError:
            return None
        if (
            run.get("user_id") != user_id
            or run.get("import_id") != import_id
            or run.get("label_set_hash") != label_set_hash
            or run.get("mode") != self.classification_mode.upper()
            or run.get("status") not in {"SUCCEEDED", "REJECTED", "FAILED"}
        ):
            return None
        return run

    def _stored_succeeded_classification(
        self,
        *,
        user_id: str,
        import_id: str,
        label_set_hash: str,
        classification_id: str,
    ) -> dict[str, Any] | None:
        run = self._stored_terminal_classification(
            user_id=user_id,
            import_id=import_id,
            label_set_hash=label_set_hash,
            classification_id=classification_id,
        )
        if run is None:
            return None
        if (
            run.get("status") != "SUCCEEDED"
            or not isinstance(run.get("response_payload"), dict)
            or (self.classification_mode == "on" and run.get("applied") is not True)
        ):
            return None
        return run

    def _recover_succeeded_classification(
        self,
        user_id: str,
        content: bytes | str,
        completion: Mapping[str, Any],
    ) -> tuple[CSVImportResult, dict[str, Any], dict[str, Any]] | None:
        import_id = str(completion["import_id"])
        label_set_hash = str(completion["label_set_hash"])
        run = self._stored_succeeded_classification(
            user_id=user_id,
            import_id=import_id,
            label_set_hash=label_set_hash,
            classification_id=str(completion["classification_id"]),
        )
        if run is None:
            return None

        recovered_result: dict[str, Any] | None = None
        recovered_completion: dict[str, Any] | None = None

        def reuse_groups(
            transactions: list[dict[str, Any]],
        ) -> list[TransactionGroup] | None:
            nonlocal recovered_completion, recovered_result
            inventory = build_label_inventory(transactions)
            if inventory.label_set_hash != label_set_hash:
                raise StorageConflict("stored classification label set changed")
            groups, recovered_result, recovered_completion = self._reuse_classification_run(
                inventory,
                import_id,
                run,
            )
            return groups

        normalized = self.csv_adapter.parse(content, group_transactions=reuse_groups)
        if recovered_result is None or recovered_completion is None:
            raise StorageConflict("stored classification success could not be recovered")
        return normalized, recovered_result, recovered_completion

    @staticmethod
    def _fallback_classification_summary(inventory: LabelInventory) -> dict[str, Any]:
        return {
            "source": "DETERMINISTIC_FALLBACK",
            "applied": False,
            "original_label_count": len(inventory.items),
            "grouped_entity_count": len(inventory.items),
            "merged_label_count": 0,
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
        result, bundle, precondition = self._prepare_candidate_decision(
            user_id,
            candidate_id,
            decision=decision,
            details=details,
        )
        try:
            self.repository.apply_record_bundle(
                user_id,
                bundle,
                candidate_preconditions=(
                    {candidate_id: precondition} if precondition is not None else None
                ),
            )
        except StorageConflict as exc:
            raise ServiceError(
                "CANDIDATE_STATE_CHANGED",
                "후보 상태가 변경되어 결정을 저장하지 않았습니다.",
                details={"candidate_id": candidate_id},
                http_status=409,
            ) from exc
        return result

    def split_candidate_group(self, user_id: str, candidate_id: str) -> dict[str, Any]:
        """Reject one AI-merged candidate and restore its exact-label alternatives."""

        expected_revision = self.repository.current_state_revision(user_id)
        candidate = self.get_record(user_id, "candidates", candidate_id)
        classification = candidate.get("classification_group")
        if not isinstance(classification, Mapping) or classification.get("can_split") is not True:
            raise ServiceError(
                "CANDIDATE_GROUP_NOT_SPLITTABLE",
                "AI가 묶은 후보만 그룹을 해제할 수 있습니다.",
                details={"candidate_id": candidate_id},
                http_status=422,
            )
        if candidate.get("status") != "PENDING":
            raise ServiceError(
                "CANDIDATE_GROUP_ALREADY_DECIDED",
                "이미 결정한 후보의 그룹은 해제할 수 없습니다.",
                details={"candidate_id": candidate_id},
                http_status=409,
            )

        evidence_ids = {
            str(transaction_id) for transaction_id in candidate.get("evidence_transaction_ids", [])
        }
        transactions = [
            transaction
            for transaction in self.repository.list_records(user_id, "transactions")
            if transaction.get("transaction_id") in evidence_ids
        ]
        if len(transactions) != len(evidence_ids):
            raise ServiceError(
                "CANDIDATE_GROUP_SPLIT_FAILED",
                "그룹을 해제하는 데 필요한 원본 거래를 찾을 수 없습니다.",
                details={"candidate_id": candidate_id},
                http_status=409,
            )

        alternatives = [
            item
            for item in self.csv_adapter.detect_candidates(transactions)
            if item.get("candidate_type") != "INSTALLMENT"
        ]
        notices = []
        for alternative in alternatives:
            notice = {
                "code": "USER_CONFIRMATION_REQUIRED",
                "severity": "INFO",
                "message": (
                    "자동 탐지 항목은 사용자가 확인하기 전까지 현금흐름에 반영되지 않습니다."
                ),
                "record_ids": [alternative["candidate_id"]],
            }
            notices.append({**notice, "notice_id": self._notice_id(notice)})

        try:
            self.repository.split_candidate_group(
                user_id,
                candidate_id,
                alternatives=alternatives,
                notices=notices,
                expected_candidate=candidate,
                expected_revision=expected_revision,
            )
        except RecordNotFound as exc:
            raise not_found("candidates", candidate_id) from exc
        except StorageConflict as exc:
            raise ServiceError(
                "CANDIDATE_GROUP_STATE_CHANGED",
                "후보 상태가 변경되어 그룹을 해제하지 않았습니다.",
                details={"candidate_id": candidate_id},
                http_status=409,
            ) from exc
        return {
            "split": True,
            "candidate_id": candidate_id,
            "candidates": alternatives,
        }

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
        candidate_preconditions: dict[str, dict[str, Any]] = {}

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
            result, bundle, precondition = self._prepare_candidate_decision(
                user_id,
                candidate_id,
                decision=str(item["decision"]),
                details=item.get("details"),
            )
            results.append(result)
            if precondition is not None:
                candidate_preconditions[candidate_id] = precondition
            for kind, records in bundle.items():
                id_field = CURRENT_KINDS[kind]
                target = prepared.setdefault(kind, {})
                for record in records:
                    target[str(record[id_field])] = record

        try:
            self.repository.apply_record_bundle(
                user_id,
                {kind: list(records.values()) for kind, records in prepared.items()},
                candidate_preconditions=candidate_preconditions,
            )
        except StorageConflict as exc:
            raise ServiceError(
                "CANDIDATE_STATE_CHANGED",
                "후보 상태가 변경되어 설정을 저장하지 않았습니다.",
                http_status=409,
            ) from exc
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
    ) -> tuple[
        dict[str, Any],
        dict[str, list[dict[str, Any]]],
        dict[str, Any] | None,
    ]:
        normalized_decision = decision.upper()
        if normalized_decision not in {"CONFIRMED", "REJECTED", "UNKNOWN"}:
            raise ServiceError(
                "INVALID_FINANCIAL_EVENT",
                "후보 확인값은 CONFIRMED, REJECTED, UNKNOWN 중 하나여야 합니다.",
                details={"decision": decision},
                http_status=422,
            )
        candidate = self.get_record(user_id, "candidates", candidate_id)
        classification = candidate.get("classification_group")
        if isinstance(classification, Mapping) and classification.get("user_split") is True:
            raise ServiceError(
                "CANDIDATE_GROUP_SUPERSEDED",
                "그룹 해제된 원래 후보는 다시 결정할 수 없습니다.",
                details={"candidate_id": candidate_id},
                http_status=409,
            )
        precondition = (
            candidate
            if isinstance(classification, Mapping) and classification.get("source") == "AI"
            else None
        )
        decided = {**candidate, "status": normalized_decision}
        if normalized_decision != "CONFIRMED":
            return decided, {"candidates": [decided]}, precondition

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
        return (
            {
                **decided,
                "promotion": {"kind": promotion_kind, "record": promoted},
            },
            bundle,
            precondition,
        )

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
