"""Strict CSV normalization for the FlowGuard MVP.

Two input shapes are supported:

* the specification's minimum transaction CSV; and
* a wide CSV whose rows are discriminated by ``record_type``.

Candidate detection is deliberately advisory. Derived recurring income, fixed
expense, and installment candidates are never promoted to confirmed financial
events without a later user decision.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

MINIMUM_TRANSACTION_COLUMNS = {
    "transaction_id",
    "account_id",
    "occurred_at",
    "direction",
    "amount",
    "description",
}

TRANSACTION_OPTIONAL_COLUMNS = {
    "category",
    "counterparty_name",
    "transaction_type",
    "card_id",
    "installment_months",
    "source_reference_id",
    "is_internal_transfer",
}

RECORD_FIELDS: dict[str, set[str]] = {
    "ACCOUNT": {
        "account_id",
        "name",
        "account_type",
        "balance",
        "minimum_balance",
        "protected_amount",
        "is_payment_account",
        "is_available_for_transfer",
        "updated_at",
    },
    "CARD": {
        "card_id",
        "name",
        "payment_account_id",
        "payment_day",
        "current_billing_amount",
        "billing_date",
        "updated_at",
    },
    "TRANSACTION": MINIMUM_TRANSACTION_COLUMNS
    | TRANSACTION_OPTIONAL_COLUMNS
    | {"source", "linked_transaction_id"},
    "COUNTERPARTY": {
        "counterparty_id",
        "name",
        "counterparty_type",
        "is_recurring",
        "payment_history_count",
        "updated_at",
    },
    "PAYMENT_HISTORY": {
        "payment_history_id",
        "counterparty_id",
        "expected_date",
        "actual_date",
        "amount",
    },
    "RECEIVABLE": {
        "receivable_id",
        "counterparty_id",
        "amount",
        "expected_date",
        "status",
        "destination_account_id",
        "user_confirmed",
        "source",
        "updated_at",
    },
    "SCHEDULED_EVENT": {
        "event_id",
        "event_type",
        "direction",
        "amount",
        "expected_date",
        "account_id",
        "counterparty_id",
        "certainty",
        "is_essential",
        "is_adjustable",
        "source",
        "source_reference_id",
    },
    "INSTALLMENT_PLAN": {
        "installment_plan_id",
        "card_id",
        "original_amount",
        "monthly_payment",
        "total_months",
        "remaining_months",
        "next_payment_date",
        "status",
    },
    "PROTECTED_FUND": {
        "protected_fund_id",
        "account_id",
        "fund_type",
        "amount",
        "release_date",
        "user_confirmed",
    },
}

RECORD_ALIASES = {
    "ACCOUNTS": "ACCOUNT",
    "CARDS": "CARD",
    "TRANSACTIONS": "TRANSACTION",
    "COUNTERPARTIES": "COUNTERPARTY",
    "PAYMENT_HISTORIES": "PAYMENT_HISTORY",
    "SCHEDULED_CASH_EVENT": "SCHEDULED_EVENT",
    "SCHEDULED_CASH_EVENTS": "SCHEDULED_EVENT",
    "SCHEDULED_EVENTS": "SCHEDULED_EVENT",
    "INSTALLMENT": "INSTALLMENT_PLAN",
    "INSTALLMENTS": "INSTALLMENT_PLAN",
    "PROTECTED_FUNDS": "PROTECTED_FUND",
    "RECEIVABLES": "RECEIVABLE",
}

REQUIRED_FIELDS: dict[str, set[str]] = {
    "ACCOUNT": {"account_id", "name", "account_type", "balance"},
    "CARD": {"card_id", "name", "payment_account_id", "payment_day", "billing_date"},
    "TRANSACTION": MINIMUM_TRANSACTION_COLUMNS,
    "COUNTERPARTY": {"counterparty_id", "name", "counterparty_type"},
    "PAYMENT_HISTORY": {
        "payment_history_id",
        "counterparty_id",
        "expected_date",
        "actual_date",
        "amount",
    },
    "RECEIVABLE": {
        "receivable_id",
        "counterparty_id",
        "amount",
        "expected_date",
        "destination_account_id",
    },
    "SCHEDULED_EVENT": {
        "event_id",
        "event_type",
        "direction",
        "amount",
        "expected_date",
        "account_id",
    },
    "INSTALLMENT_PLAN": {
        "installment_plan_id",
        "card_id",
        "original_amount",
        "monthly_payment",
        "total_months",
        "remaining_months",
        "next_payment_date",
    },
    "PROTECTED_FUND": {
        "protected_fund_id",
        "account_id",
        "fund_type",
        "amount",
    },
}

KIND_TO_COLLECTION = {
    "ACCOUNT": "accounts",
    "CARD": "cards",
    "TRANSACTION": "transactions",
    "COUNTERPARTY": "counterparties",
    "PAYMENT_HISTORY": "payment_histories",
    "RECEIVABLE": "receivables",
    "SCHEDULED_EVENT": "scheduled_events",
    "INSTALLMENT_PLAN": "installment_plans",
    "PROTECTED_FUND": "protected_funds",
}

ID_FIELDS = {
    "ACCOUNT": "account_id",
    "CARD": "card_id",
    "TRANSACTION": "transaction_id",
    "COUNTERPARTY": "counterparty_id",
    "PAYMENT_HISTORY": "payment_history_id",
    "RECEIVABLE": "receivable_id",
    "SCHEDULED_EVENT": "event_id",
    "INSTALLMENT_PLAN": "installment_plan_id",
    "PROTECTED_FUND": "protected_fund_id",
}

INTEGER_FIELDS = {
    "amount",
    "balance",
    "minimum_balance",
    "protected_amount",
    "payment_day",
    "current_billing_amount",
    "payment_history_count",
    "installment_months",
    "original_amount",
    "monthly_payment",
    "total_months",
    "remaining_months",
}

BOOLEAN_FIELDS = {
    "is_payment_account",
    "is_available_for_transfer",
    "is_recurring",
    "user_confirmed",
    "is_essential",
    "is_adjustable",
    "is_internal_transfer",
}

DATE_FIELDS = {
    "actual_date",
    "billing_date",
    "expected_date",
    "next_payment_date",
    "release_date",
}
DATETIME_FIELDS = {"occurred_at", "updated_at"}
AMOUNT_FIELDS = {
    "amount",
    "balance",
    "minimum_balance",
    "protected_amount",
    "current_billing_amount",
    "original_amount",
    "monthly_payment",
}

ENUM_VALUES = {
    "account_type": {"CHECKING", "SAVINGS", "OTHER"},
    "direction": {"INFLOW", "OUTFLOW"},
    "counterparty_type": {"CLIENT", "MERCHANT", "PLATFORM", "OTHER"},
    "event_type": {
        "RECEIVABLE",
        "CARD_BILL",
        "INSTALLMENT_PAYMENT",
        "RENT",
        "INSURANCE",
        "UTILITY",
        "LOAN_PAYMENT",
        "TAX",
        "SAVINGS",
        "DISCRETIONARY_EXPENSE",
        "OTHER_INFLOW",
        "OTHER_OUTFLOW",
    },
    "transaction_type": {
        "PURCHASE",
        "INCOME",
        "TRANSFER",
        "CARD_PAYMENT",
        "LOAN_PAYMENT",
        "TAX",
        "REFUND",
        "OTHER",
    },
    "certainty": {"CONFIRMED", "ESTIMATED", "UNCERTAIN"},
    "fund_type": {"TAX_RESERVE", "EMERGENCY_RESERVE", "BUSINESS_RESERVE", "OTHER"},
    "source": {"CSV", "USER_INPUT", "USER_CONFIRMED", "SYSTEM_DERIVED"},
}


class CSVImportError(ValueError):
    """A stable, structured import failure."""

    code = "INVALID_CSV_FORMAT"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


@dataclass(slots=True)
class CSVImportResult:
    """Normalized records and advisory observations from one import."""

    accounts: list[dict[str, Any]] = field(default_factory=list)
    cards: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    counterparties: list[dict[str, Any]] = field(default_factory=list)
    payment_histories: list[dict[str, Any]] = field(default_factory=list)
    scheduled_events: list[dict[str, Any]] = field(default_factory=list)
    receivables: list[dict[str, Any]] = field(default_factory=list)
    installment_plans: list[dict[str, Any]] = field(default_factory=list)
    protected_funds: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    data_quality_notices: list[dict[str, Any]] = field(default_factory=list)
    format: str = "MINIMUM_TRANSACTION"

    def records(self) -> dict[str, list[dict[str, Any]]]:
        return {
            key: getattr(self, key)
            for key in (
                "accounts",
                "cards",
                "transactions",
                "counterparties",
                "payment_histories",
                "scheduled_events",
                "receivables",
                "installment_plans",
                "protected_funds",
                "candidates",
            )
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            **self.records(),
            "data_quality_notices": self.data_quality_notices,
        }


@dataclass(slots=True)
class TransactionGroup:
    """Transactions that share one deterministic or validated AI label identity."""

    direction: str
    key: str
    transactions: list[dict[str, Any]]
    classification: dict[str, Any] | None = None


class CSVFinancialDataAdapter:
    """Parse and normalize a strict FlowGuard CSV."""

    def parse(
        self,
        content: bytes | str,
        *,
        group_transactions: Callable[[list[dict[str, Any]]], list[TransactionGroup] | None]
        | None = None,
    ) -> CSVImportResult:
        text = self._decode(content)
        reader = csv.DictReader(io.StringIO(text), strict=True)
        if reader.fieldnames is None:
            raise CSVImportError("CSV 헤더가 없습니다.")

        headers = [header.strip() for header in reader.fieldnames if header is not None]
        if not headers or any(not header for header in headers):
            raise CSVImportError("빈 CSV 컬럼 이름은 허용되지 않습니다.")
        if len(headers) != len(set(headers)):
            raise CSVImportError("중복된 CSV 컬럼 이름은 허용되지 않습니다.")

        is_wide = "record_type" in headers
        allowed = (
            {"record_type"} | set().union(*RECORD_FIELDS.values())
            if is_wide
            else MINIMUM_TRANSACTION_COLUMNS | TRANSACTION_OPTIONAL_COLUMNS
        )
        unknown = sorted(set(headers) - allowed)
        if unknown:
            raise CSVImportError(
                "지원하지 않는 CSV 컬럼이 있습니다.",
                details={"unknown_columns": unknown},
            )
        missing = [] if is_wide else sorted(MINIMUM_TRANSACTION_COLUMNS - set(headers))
        if missing:
            raise CSVImportError(
                "필수 거래 컬럼이 누락되었습니다.",
                details={"missing_columns": missing},
            )

        rows: list[tuple[int, dict[str, str | None]]] = []
        try:
            for line_number, raw in enumerate(reader, start=2):
                if None in raw:
                    raise CSVImportError(
                        "헤더보다 많은 값이 있는 행입니다.",
                        details={"line": line_number},
                    )
                row = {
                    (key.strip() if key else ""): value.strip() if value else None
                    for key, value in raw.items()
                }
                if not any(row.values()):
                    continue
                rows.append((line_number, row))
        except csv.Error as exc:
            raise CSVImportError("CSV 구문이 올바르지 않습니다.") from exc

        if not rows:
            raise CSVImportError("CSV에 데이터 행이 없습니다.")

        result = CSVImportResult(format="WIDE_RECORDS" if is_wide else "MINIMUM_TRANSACTION")
        seen: dict[tuple[str, str], dict[str, Any]] = {}

        for line_number, row in rows:
            record_type = "TRANSACTION"
            if is_wide:
                raw_type = (row.get("record_type") or "").upper()
                record_type = RECORD_ALIASES.get(raw_type, raw_type)
                if record_type not in RECORD_FIELDS:
                    raise CSVImportError(
                        "지원하지 않는 record_type입니다.",
                        details={"line": line_number, "record_type": raw_type or None},
                    )
                unrelated = sorted(
                    key
                    for key, value in row.items()
                    if value is not None
                    and key != "record_type"
                    and key not in RECORD_FIELDS[record_type]
                )
                if unrelated:
                    raise CSVImportError(
                        "record_type과 관련 없는 필드에 값이 있습니다.",
                        details={
                            "line": line_number,
                            "record_type": record_type,
                            "fields": unrelated,
                        },
                    )

            normalized = self._normalize_row(record_type, row, line_number)
            identity = (record_type, str(normalized[ID_FIELDS[record_type]]))
            previous = seen.get(identity)
            if previous is not None:
                if previous != normalized:
                    raise CSVImportError(
                        "같은 식별자에 서로 다른 데이터가 있습니다.",
                        details={
                            "line": line_number,
                            "record_type": record_type,
                            "record_id": identity[1],
                        },
                    )
                result.data_quality_notices.append(
                    self._notice(
                        "DUPLICATE_RECORD_IGNORED",
                        "동일한 레코드가 중복되어 한 번만 반영했습니다.",
                        [identity[1]],
                        severity="WARNING",
                    )
                )
                continue
            seen[identity] = normalized
            getattr(result, KIND_TO_COLLECTION[record_type]).append(normalized)

        groups = group_transactions(result.transactions) if group_transactions else None
        result.candidates.extend(self._detect_candidates(result.transactions, groups=groups))
        result.data_quality_notices.extend(self._quality_notices(result, is_wide=is_wide))
        return result

    @staticmethod
    def _decode(content: bytes | str) -> str:
        if isinstance(content, str):
            return content.removeprefix("\ufeff")
        try:
            return content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CSVImportError(
                "CSV는 UTF-8 인코딩이어야 합니다.",
                details={"encoding": "UTF-8"},
            ) from exc

    def _normalize_row(
        self,
        record_type: str,
        row: dict[str, str | None],
        line_number: int,
    ) -> dict[str, Any]:
        fields = RECORD_FIELDS[record_type]
        missing = sorted(name for name in REQUIRED_FIELDS[record_type] if not row.get(name))
        if missing:
            raise CSVImportError(
                "필수 필드 값이 누락되었습니다.",
                details={
                    "line": line_number,
                    "record_type": record_type,
                    "missing_fields": missing,
                },
            )

        normalized: dict[str, Any] = {}
        for name in fields:
            raw = row.get(name)
            if raw is None:
                continue
            try:
                normalized[name] = self._normalize_value(name, raw)
            except ValueError as exc:
                raise CSVImportError(
                    "필드 값이 올바르지 않습니다.",
                    details={
                        "line": line_number,
                        "record_type": record_type,
                        "field": name,
                        "value": raw,
                        "reason": str(exc),
                    },
                ) from exc

        self._apply_explicit_defaults(record_type, normalized)
        self._validate_record(record_type, normalized, line_number)
        return normalized

    @staticmethod
    def _normalize_value(name: str, raw: str) -> Any:
        if name in INTEGER_FIELDS:
            if raw.startswith("+") or not raw.isdigit():
                raise ValueError("0 이상의 쉼표 없는 정수여야 합니다.")
            return int(raw)
        if name in BOOLEAN_FIELDS:
            lowered = raw.lower()
            if lowered not in {"true", "false"}:
                raise ValueError("true 또는 false여야 합니다.")
            return lowered == "true"
        if name in DATE_FIELDS:
            return date.fromisoformat(raw).isoformat()
        if name in DATETIME_FIELDS:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError(f"{name}에는 시간대가 포함되어야 합니다.")
            return parsed.isoformat()
        if name in ENUM_VALUES:
            value = raw.upper()
            if value not in ENUM_VALUES[name]:
                raise ValueError(f"허용값: {', '.join(sorted(ENUM_VALUES[name]))}")
            return value
        return raw

    @staticmethod
    def _apply_explicit_defaults(record_type: str, record: dict[str, Any]) -> None:
        defaults: dict[str, dict[str, Any]] = {
            "ACCOUNT": {
                "minimum_balance": 0,
                "protected_amount": 0,
                "is_payment_account": False,
                "is_available_for_transfer": True,
            },
            "TRANSACTION": {
                "transaction_type": "OTHER",
                "source": "CSV",
                "is_internal_transfer": False,
            },
            "COUNTERPARTY": {"is_recurring": False, "payment_history_count": 0},
            "RECEIVABLE": {
                "status": "ESTIMATED",
                "user_confirmed": False,
                "source": "CSV",
            },
            "SCHEDULED_EVENT": {
                "certainty": "ESTIMATED",
                "is_essential": False,
                "is_adjustable": False,
                "source": "CSV",
            },
            "INSTALLMENT_PLAN": {"status": "ACTIVE"},
            "PROTECTED_FUND": {"user_confirmed": False},
        }
        for name, value in defaults.get(record_type, {}).items():
            record.setdefault(name, value)

    @staticmethod
    def _validate_record(
        record_type: str,
        record: dict[str, Any],
        line_number: int,
    ) -> None:
        negative = [name for name in AMOUNT_FIELDS if record.get(name, 0) < 0]
        if negative:
            raise CSVImportError(
                "금액 필드는 0 이상이어야 합니다.",
                details={"line": line_number, "fields": sorted(negative)},
            )
        if record_type in {
            "TRANSACTION",
            "SCHEDULED_EVENT",
            "RECEIVABLE",
            "PAYMENT_HISTORY",
        }:
            amount = record.get("amount", 0)
            if amount <= 0:
                raise CSVImportError(
                    "거래 및 이벤트 금액은 0보다 커야 합니다.",
                    details={"line": line_number, "field": "amount"},
                )
        if record_type == "ACCOUNT" and record["protected_amount"] > record["balance"]:
            raise CSVImportError(
                "보호금액은 계좌 잔액보다 클 수 없습니다.",
                details={"line": line_number, "field": "protected_amount"},
            )
        if record_type == "CARD" and not 1 <= record["payment_day"] <= 31:
            raise CSVImportError(
                "카드 결제일은 1~31이어야 합니다.",
                details={"line": line_number, "field": "payment_day"},
            )
        if record_type == "INSTALLMENT_PLAN":
            positive_fields = (
                "original_amount",
                "monthly_payment",
                "total_months",
            )
            if any(record[name] <= 0 for name in positive_fields):
                raise CSVImportError(
                    "할부 금액과 전체 개월은 0보다 커야 합니다.",
                    details={"line": line_number, "fields": list(positive_fields)},
                )
            if record["remaining_months"] > record["total_months"]:
                raise CSVImportError(
                    "남은 할부 개월은 전체 개월보다 클 수 없습니다.",
                    details={"line": line_number, "field": "remaining_months"},
                )
        if record_type == "PROTECTED_FUND" and record["amount"] <= 0:
            raise CSVImportError(
                "보호자금은 0보다 커야 합니다.",
                details={"line": line_number, "field": "amount"},
            )
        allowed_statuses = {
            "RECEIVABLE": {
                "CONFIRMED",
                "ESTIMATED",
                "OVERDUE",
                "RECEIVED",
                "CANCELLED",
            },
            "INSTALLMENT_PLAN": {"ACTIVE", "COMPLETED", "CANCELLED"},
        }
        if record_type in allowed_statuses:
            status = str(record["status"]).upper()
            if status not in allowed_statuses[record_type]:
                raise CSVImportError(
                    "레코드 상태값이 올바르지 않습니다.",
                    details={
                        "line": line_number,
                        "record_type": record_type,
                        "field": "status",
                        "value": record["status"],
                    },
                )
            record["status"] = status

    @staticmethod
    def deterministic_grouping(transactions: list[dict[str, Any]]) -> list[TransactionGroup]:
        """Preserve the original exact-label grouping as the mandatory fallback."""

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for transaction in transactions:
            label = transaction.get("counterparty_name") or transaction.get("description")
            if label:
                grouped[(transaction["direction"], str(label).strip().casefold())].append(
                    transaction
                )
        return [
            TransactionGroup(
                direction=direction,
                key=normalized_label,
                transactions=group,
            )
            for (direction, normalized_label), group in sorted(grouped.items())
        ]

    def _detect_candidates(
        self,
        transactions: list[dict[str, Any]],
        *,
        groups: list[TransactionGroup] | None = None,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for transaction in transactions:
            months = transaction.get("installment_months")
            if isinstance(months, int) and months > 1:
                candidates.append(
                    self._candidate(
                        "INSTALLMENT",
                        [transaction],
                        confidence=1.0,
                        proposed={
                            "card_id": transaction.get("card_id"),
                            "original_amount": transaction["amount"],
                            "total_months": months,
                            "source_transaction_id": transaction["transaction_id"],
                        },
                    )
                )

        selected_groups = (
            groups if groups is not None else self.deterministic_grouping(transactions)
        )
        for transaction_group in selected_groups:
            direction = transaction_group.direction
            ordered = sorted(
                transaction_group.transactions,
                key=lambda item: item["occurred_at"],
            )
            minimum_count = 3 if direction == "INFLOW" else 2
            if len(ordered) < minimum_count or not self._has_recurring_spacing(ordered):
                continue
            amounts = [item["amount"] for item in ordered]
            median_amount = int(statistics.median(amounts))
            tolerance = max(1, int(median_amount * 0.10))
            if any(abs(amount - median_amount) > tolerance for amount in amounts):
                continue
            candidate_type = "RECURRING_INCOME" if direction == "INFLOW" else "FIXED_EXPENSE"
            next_expected_date = self._next_expected_date(ordered)
            classification = transaction_group.classification
            counterparty_name = (
                classification.get("normalized_name")
                if classification is not None
                else ordered[-1].get("counterparty_name")
            )
            counterparty_id = self._derived_counterparty_id(
                counterparty_name or ordered[-1].get("description") or "unknown"
            )
            event_type = self._fixed_event_type(ordered[-1])
            is_essential = self._looks_essential(ordered[-1])
            if classification is not None and direction == "OUTFLOW":
                category_hint = str(classification["category_hint"])
                # Card bills and installment payments require a concrete card
                # or plan reference for core cashflow deduplication. Keep those
                # AI values as metadata until that structural link exists.
                if category_hint not in {"CARD_BILL", "INSTALLMENT_PAYMENT"}:
                    event_type = category_hint
                essential_hint = classification.get("essential_hint")
                if isinstance(essential_hint, bool):
                    is_essential = essential_hint
            candidates.append(
                self._candidate(
                    candidate_type,
                    ordered,
                    confidence=min(0.95, 0.55 + 0.1 * len(ordered)),
                    proposed={
                        "amount": median_amount,
                        "counterparty_id": counterparty_id,
                        "counterparty_name": counterparty_name,
                        "description": ordered[-1].get("description"),
                        "direction": direction,
                        "account_id": ordered[-1]["account_id"],
                        "destination_account_id": ordered[-1]["account_id"],
                        "expected_date": next_expected_date.isoformat(),
                        "event_type": event_type,
                        "is_essential": is_essential,
                    },
                    classification_group=classification,
                )
            )
        return candidates

    def detect_candidates(
        self,
        transactions: list[dict[str, Any]],
        *,
        groups: list[TransactionGroup] | None = None,
    ) -> list[dict[str, Any]]:
        """Expose candidate detection for a user-requested AI-group split."""

        return self._detect_candidates(transactions, groups=groups)

    @staticmethod
    def _next_expected_date(transactions: list[dict[str, Any]]) -> date:
        dates = [datetime.fromisoformat(item["occurred_at"]).date() for item in transactions]
        latest = dates[-1]
        month_index = latest.month
        year = latest.year + month_index // 12
        month = month_index % 12 + 1
        day = min(latest.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    @staticmethod
    def _derived_counterparty_id(label: str) -> str:
        digest = hashlib.sha256(label.strip().casefold().encode()).hexdigest()[:16]
        return f"counterparty-{digest}"

    @staticmethod
    def _fixed_event_type(transaction: dict[str, Any]) -> str:
        text = " ".join(
            str(transaction.get(key) or "")
            for key in ("category", "description", "counterparty_name")
        ).casefold()
        mappings = (
            (("월세", "rent"), "RENT"),
            (("보험", "insurance"), "INSURANCE"),
            (("공과", "전기", "수도", "utility"), "UTILITY"),
            (("대출", "loan"), "LOAN_PAYMENT"),
            (("세금", "tax"), "TAX"),
        )
        return next(
            (
                event_type
                for keywords, event_type in mappings
                if any(keyword in text for keyword in keywords)
            ),
            "OTHER_OUTFLOW",
        )

    @staticmethod
    def _looks_essential(transaction: dict[str, Any]) -> bool:
        return CSVFinancialDataAdapter._fixed_event_type(transaction) in {
            "RENT",
            "INSURANCE",
            "UTILITY",
            "LOAN_PAYMENT",
            "TAX",
        }

    @staticmethod
    def _has_recurring_spacing(transactions: list[dict[str, Any]]) -> bool:
        dates = [datetime.fromisoformat(item["occurred_at"]).date() for item in transactions]
        gaps = [(right - left).days for left, right in zip(dates, dates[1:], strict=False)]
        return bool(gaps) and all(20 <= gap <= 40 for gap in gaps)

    @staticmethod
    def _candidate(
        candidate_type: str,
        transactions: Iterable[dict[str, Any]],
        *,
        confidence: float,
        proposed: dict[str, Any],
        classification_group: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        transaction_ids = [item["transaction_id"] for item in transactions]
        digest = hashlib.sha256(
            f"{candidate_type}:{':'.join(transaction_ids)}".encode()
        ).hexdigest()[:16]
        candidate = {
            "candidate_id": f"candidate-{digest}",
            "candidate_type": candidate_type,
            "status": "PENDING",
            "confidence": round(confidence, 2),
            "evidence_transaction_ids": transaction_ids,
            "proposed_record": proposed,
        }
        if classification_group is not None:
            candidate["classification_group"] = classification_group
        return candidate

    def _quality_notices(self, result: CSVImportResult, *, is_wide: bool) -> list[dict[str, Any]]:
        notices: list[dict[str, Any]] = []
        if not is_wide:
            account_ids = sorted({item["account_id"] for item in result.transactions})
            notices.append(
                self._notice(
                    "ACCOUNT_DETAILS_REQUIRED",
                    "최소 거래 CSV에는 계좌 잔액과 보호 설정이 없어 분석 전에 확인이 필요합니다.",
                    account_ids,
                    severity="ERROR",
                )
            )
        if len(result.transactions) < 3:
            notices.append(
                self._notice(
                    "INSUFFICIENT_HISTORY_FOR_CANDIDATES",
                    "반복 수입과 고정지출을 신뢰성 있게 탐지하기에 거래 이력이 부족합니다.",
                    [item["transaction_id"] for item in result.transactions],
                    severity="WARNING",
                )
            )
        missing_counterparty = [
            item["transaction_id"]
            for item in result.transactions
            if not item.get("counterparty_name")
        ]
        if missing_counterparty:
            notices.append(
                self._notice(
                    "MISSING_COUNTERPARTY",
                    "일부 거래에 거래처 정보가 없어 반복성 판단의 신뢰도가 낮습니다.",
                    missing_counterparty,
                    severity="WARNING",
                )
            )
        missing_updated_at: list[str] = []
        for kind, id_field in (
            ("accounts", "account_id"),
            ("cards", "card_id"),
            ("counterparties", "counterparty_id"),
            ("receivables", "receivable_id"),
        ):
            missing_updated_at.extend(
                item[id_field] for item in getattr(result, kind) if "updated_at" not in item
            )
        if missing_updated_at:
            notices.append(
                self._notice(
                    "MISSING_UPDATED_AT",
                    "일부 레코드의 최신시각이 없어 분석 기준시각을 사용해야 합니다.",
                    missing_updated_at,
                    severity="WARNING",
                )
            )
        for candidate in result.candidates:
            notices.append(
                self._notice(
                    "USER_CONFIRMATION_REQUIRED",
                    "자동 탐지 항목은 사용자가 확인하기 전까지 현금흐름에 반영되지 않습니다.",
                    [candidate["candidate_id"]],
                    severity="INFO",
                )
            )
        return notices

    @staticmethod
    def _notice(
        code: str,
        message: str,
        record_ids: list[str],
        *,
        severity: str,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "record_ids": record_ids,
        }
