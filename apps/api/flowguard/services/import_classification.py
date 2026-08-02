"""Privacy-minimized label inventory and validated group projection for CSV imports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from flowguard.adapters import CSVFinancialDataAdapter, TransactionGroup
from flowguard.ai_contract import LabelClassificationResponse
from flowguard.ai_numeric_policy import redact_numeric_expressions
from flowguard.config import (
    AI_CLASSIFICATION_CONTRACT_VERSION,
    AI_CLASSIFICATION_PROMPT_VERSION,
    AI_CLASSIFICATION_SCHEMA_VERSION,
)


@dataclass(frozen=True)
class LabelInventoryItem:
    label_id: str
    raw_text: str
    safe_text: str
    direction: str
    occurrences: int
    transaction_group: TransactionGroup


@dataclass(frozen=True)
class LabelInventory:
    items: tuple[LabelInventoryItem, ...]
    label_set_hash: str

    @property
    def by_id(self) -> dict[str, LabelInventoryItem]:
        return {item.label_id: item for item in self.items}

    def request_payload(
        self,
        *,
        import_id: str,
        locale: str,
        retry_after: str | None = None,
        request_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is None:
            idempotency_key = (
                f"{import_id}:{self.label_set_hash}:"
                f"contract-{AI_CLASSIFICATION_CONTRACT_VERSION}:"
                f"{AI_CLASSIFICATION_PROMPT_VERSION}:{locale}"
            )
            if retry_after is not None:
                idempotency_key = f"{idempotency_key}:retry-{retry_after}"
        request_id = request_id or f"ai-classify-{uuid5(NAMESPACE_URL, idempotency_key)}"
        return {
            "schemaVersion": AI_CLASSIFICATION_SCHEMA_VERSION,
            "contractVersion": AI_CLASSIFICATION_CONTRACT_VERSION,
            "promptVersion": AI_CLASSIFICATION_PROMPT_VERSION,
            "requestId": request_id,
            "idempotencyKey": idempotency_key,
            "importId": import_id,
            "locale": locale,
            "labels": [
                {
                    "labelId": item.label_id,
                    "text": item.safe_text,
                    "direction": item.direction,
                    "occurrences": item.occurrences,
                }
                for item in self.items
            ],
        }


def build_label_inventory(transactions: list[dict[str, Any]]) -> LabelInventory:
    """Build opaque label IDs without exposing transaction IDs, dates, or amounts."""

    items: list[LabelInventoryItem] = []
    sensitive_identifiers = {
        str(value)
        for transaction in transactions
        for field, value in transaction.items()
        if field.endswith("_id") and value
    }
    for index, group in enumerate(
        CSVFinancialDataAdapter.deterministic_grouping(transactions),
        start=1,
    ):
        representative = group.transactions[0]
        raw_label = str(
            representative.get("counterparty_name") or representative.get("description")
        ).strip()
        safe_label = redact_financial_numbers(
            raw_label,
            sensitive_identifiers=sensitive_identifiers,
        )
        items.append(
            LabelInventoryItem(
                label_id=f"label-{index}",
                raw_text=raw_label,
                safe_text=safe_label,
                direction=group.direction,
                occurrences=len(group.transactions),
                transaction_group=group,
            )
        )
    hash_input = [
        {
            "text": item.safe_text,
            "direction": item.direction,
            "occurrences": item.occurrences,
        }
        for item in items
    ]
    label_set_hash = hashlib.sha256(
        json.dumps(
            hash_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return LabelInventory(items=tuple(items), label_set_hash=label_set_hash)


def classification_import_id(
    *,
    user_id: str,
    mode: str,
    label_set_hash: str,
    locale: str,
) -> str:
    """Create a retry-stable, mode-scoped import identity from non-financial inputs."""

    digest = hashlib.sha256(
        (
            f"{user_id}\0{mode}\0{label_set_hash}\0"
            f"{AI_CLASSIFICATION_SCHEMA_VERSION}\0"
            f"{AI_CLASSIFICATION_CONTRACT_VERSION}\0"
            f"{AI_CLASSIFICATION_PROMPT_VERSION}\0{locale}"
        ).encode()
    ).hexdigest()[:32]
    return f"import-{digest}"


def redact_financial_numbers(
    label: str,
    *,
    sensitive_identifiers: set[str] | None = None,
) -> str:
    """Remove embedded identifiers, amounts, and dates from an AI-bound label."""

    redacted = label
    for identifier in sorted(sensitive_identifiers or (), key=len, reverse=True):
        escaped = re.escape(identifier)
        if len(identifier) <= 2 and identifier.isalnum():
            escaped = rf"(?<![\w-]){escaped}(?![\w-])"
        redacted = re.sub(escaped, "<ID>", redacted, flags=re.IGNORECASE)
    return redact_numeric_expressions(redacted)


def project_validated_groups(
    inventory: LabelInventory,
    response_payload: dict[str, Any],
) -> list[TransactionGroup]:
    """Convert a fully validated AI response into the adapter's grouping boundary."""

    response = LabelClassificationResponse.model_validate(response_payload)
    inventory_by_id = inventory.by_id
    groups: list[TransactionGroup] = []
    for group in sorted(response.groups, key=lambda item: item.groupId):
        items = [inventory_by_id[label_id] for label_id in group.labelIds]
        transactions = sorted(
            [transaction for item in items for transaction in item.transaction_group.transactions],
            key=lambda item: (item["occurred_at"], item["transaction_id"]),
        )
        groups.append(
            TransactionGroup(
                direction=items[0].direction,
                key=group.groupId,
                transactions=transactions,
                classification={
                    "source": "AI",
                    "group_id": group.groupId,
                    "normalized_name": group.normalizedName,
                    "entity_kind": group.entityKind.value,
                    "category_hint": group.categoryHint.value,
                    "essential_hint": group.essentialHint,
                    "confidence": group.confidence.value,
                    "reason": group.reason,
                    "labels": [item.raw_text for item in items],
                    "can_split": len(items) > 1,
                },
            )
        )
    for label_id in sorted(response.ungrouped):
        item = inventory_by_id[label_id]
        groups.append(item.transaction_group)
    return groups


def classification_summary(
    inventory: LabelInventory,
    response_payload: dict[str, Any],
    *,
    applied: bool,
) -> dict[str, Any]:
    response = LabelClassificationResponse.model_validate(response_payload)
    grouped_entity_count = len(response.groups) + len(response.ungrouped)
    return {
        "source": "AI" if applied else "AI_SHADOW",
        "applied": applied,
        "original_label_count": len(inventory.items),
        "grouped_entity_count": grouped_entity_count,
        "merged_label_count": max(0, len(inventory.items) - grouped_entity_count),
    }


__all__ = [
    "LabelInventory",
    "LabelInventoryItem",
    "build_label_inventory",
    "classification_import_id",
    "classification_summary",
    "project_validated_groups",
    "redact_financial_numbers",
]
