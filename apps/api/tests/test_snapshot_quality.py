from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flowguard.services.snapshots import SnapshotBuilder
from flowguard.storage import FlowGuardRepository


def _account(updated_at: str | None) -> dict[str, object]:
    account: dict[str, object] = {
        "account_id": "account-1",
        "name": "결제계좌",
        "account_type": "CHECKING",
        "balance": 500_000,
        "minimum_balance": 0,
        "protected_amount": 0,
        "is_payment_account": True,
        "is_available_for_transfer": True,
    }
    if updated_at is not None:
        account["updated_at"] = updated_at
    return account


def test_snapshot_preserves_supplied_freshness_without_quality_penalty() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    supplied = "2026-07-24T08:30:00+09:00"
    repository.upsert_records("user", "accounts", [_account(supplied)])

    snapshot = SnapshotBuilder(repository).build(
        "user",
        as_of=datetime(2026, 7, 24, 9, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert snapshot.accounts[0].updated_at.isoformat() == supplied
    assert not any(
        item.startswith("derived_updated_at:") for item in snapshot.data_quality.unconfirmed_items
    )


def test_snapshot_marks_only_missing_freshness_as_derived() -> None:
    repository = FlowGuardRepository("sqlite:///:memory:")
    repository.upsert_records("user", "accounts", [_account(None)])

    snapshot = SnapshotBuilder(repository).build(
        "user",
        as_of=datetime(2026, 7, 24, 9, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert snapshot.accounts[0].updated_at == snapshot.as_of
    assert snapshot.data_quality.unconfirmed_items == ("derived_updated_at:accounts:account-1",)
