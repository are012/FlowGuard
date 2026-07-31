"""SQLAlchemy persistence for current data, immutable analysis inputs, and audit state."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel as PydanticModel
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool

DEFAULT_DATABASE_URL = "sqlite:///./flowguard.db"

CURRENT_KINDS = {
    "accounts": "account_id",
    "cards": "card_id",
    "transactions": "transaction_id",
    "counterparties": "counterparty_id",
    "payment_histories": "payment_history_id",
    "scheduled_events": "event_id",
    "receivables": "receivable_id",
    "installment_plans": "installment_plan_id",
    "protected_funds": "protected_fund_id",
    "candidates": "candidate_id",
    "data_quality_notices": "notice_id",
    "preferences": "preference_id",
}

ANALYSIS_STATUSES = (
    "QUEUED",
    "SNAPSHOT_BUILDING",
    "BASELINE_ANALYZING",
    "AGENT_INVESTIGATING",
    "PLAN_EVALUATING",
    "REPORT_BUILDING",
    "COMPLETED",
    "FAILED",
)

ANALYSIS_TRANSITIONS = {
    "QUEUED": {"SNAPSHOT_BUILDING", "FAILED"},
    "SNAPSHOT_BUILDING": {"BASELINE_ANALYZING", "FAILED"},
    "BASELINE_ANALYZING": {"AGENT_INVESTIGATING", "FAILED"},
    "AGENT_INVESTIGATING": {"PLAN_EVALUATING", "FAILED"},
    "PLAN_EVALUATING": {"REPORT_BUILDING", "FAILED"},
    "REPORT_BUILDING": {"COMPLETED", "FAILED"},
    "COMPLETED": set(),
    "FAILED": set(),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def jsonable(value: Any) -> Any:
    """Convert supported application objects into JSON-compatible values."""

    if isinstance(value, PydanticModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


class StorageError(RuntimeError):
    """Base persistence-layer failure."""


class RecordNotFound(StorageError):
    """The requested persisted entity does not exist."""


class StorageConflict(StorageError):
    """An immutable or uniquely keyed entity already exists."""


class InvalidAnalysisTransition(StorageError):
    """An analysis status transition violated the workflow."""


class Base(DeclarativeBase):
    pass


class CurrentRecordRow(Base):
    __tablename__ = "current_records"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    __table_args__ = (Index("ix_current_records_user_kind", "user_id", "kind"),)


class FinancialSnapshotRow(Base):
    __tablename__ = "financial_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class AnalysisRunRow(Base):
    __tablename__ = "analysis_runs"

    analysis_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("financial_snapshots.snapshot_id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    events: Mapped[list[AnalysisEventRow]] = relationship(
        back_populates="analysis",
        order_by="AnalysisEventRow.sequence",
        cascade="all, delete-orphan",
    )


class AnalysisEventRow(Base):
    __tablename__ = "analysis_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.analysis_id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    analysis: Mapped[AnalysisRunRow] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("analysis_id", "sequence", name="uq_analysis_event_sequence"),
    )


class AnalysisReportRow(Base):
    __tablename__ = "analysis_reports"

    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.analysis_id"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("financial_snapshots.snapshot_id"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, index=True
    )


class LatestReportPointerRow(Base):
    __tablename__ = "latest_report_pointers"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_reports.analysis_id"), nullable=False, unique=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class RecommendationRow(Base):
    __tablename__ = "recommendations"

    recommendation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.analysis_id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PENDING")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ApprovalAuditRow(Base):
    __tablename__ = "approval_records"

    audit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    recommendation_id: Mapped[str] = mapped_column(
        ForeignKey("recommendations.recommendation_id"), nullable=False, index=True
    )
    analysis_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)
    effect: Mapped[str] = mapped_column(String(40), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"

    tool_execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.analysis_id"), nullable=False, index=True
    )
    agent_run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "agent_run_id",
            "sequence",
            name="uq_tool_execution_sequence",
        ),
    )


class AIInterpretationRow(Base):
    __tablename__ = "ai_interpretations"

    ai_request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.analysis_id"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reuse_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "snapshot_revision",
            "contract_version",
            "prompt_version",
            name="uq_ai_interpretation_request",
        ),
    )


class FlowGuardRepository:
    """Repository whose persisted boundaries are plain JSON-compatible mappings."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        create_schema: bool = True,
    ) -> None:
        self.database_url = database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
        self.engine = engine or self._create_engine(self.database_url)
        self._session_factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )
        if create_schema:
            self.create_schema()

    @staticmethod
    def _create_engine(database_url: str) -> Engine:
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if database_url in {"sqlite://", "sqlite:///:memory:"}:
                kwargs["poolclass"] = StaticPool
        return create_engine(database_url, **kwargs)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def _session(self) -> Iterable[Session]:
        with self._session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def upsert_records(self, user_id: str, kind: str, records: Iterable[Mapping[str, Any]]) -> int:
        id_field = self._id_field(kind)
        count = 0
        with self._session() as session:
            for raw in records:
                payload = jsonable(raw)
                record_id = payload.get(id_field)
                if not isinstance(record_id, str) or not record_id:
                    raise StorageError(f"{kind} record requires {id_field}")
                row = session.get(
                    CurrentRecordRow,
                    {"user_id": user_id, "kind": kind, "record_id": record_id},
                )
                if row is None:
                    session.add(
                        CurrentRecordRow(
                            user_id=user_id,
                            kind=kind,
                            record_id=record_id,
                            payload=payload,
                        )
                    )
                else:
                    row.payload = payload
                    row.updated_at = utc_now()
                count += 1
        return count

    def apply_record_bundle(
        self,
        user_id: str,
        bundle: Mapping[str, Iterable[Mapping[str, Any]]],
        *,
        replace_kinds: set[str] | None = None,
    ) -> dict[str, int]:
        """Apply a multi-kind import in one transaction."""

        replace_kinds = replace_kinds or set()
        prepared: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        for kind, records in bundle.items():
            id_field = self._id_field(kind)
            payloads = [jsonable(item) for item in records]
            for payload in payloads:
                record_id = payload.get(id_field)
                if not isinstance(record_id, str) or not record_id:
                    raise StorageError(f"{kind} record requires {id_field}")
            prepared[kind] = (id_field, payloads)

        with self._session() as session:
            for kind, (id_field, payloads) in prepared.items():
                if kind in replace_kinds:
                    session.execute(
                        delete(CurrentRecordRow).where(
                            CurrentRecordRow.user_id == user_id,
                            CurrentRecordRow.kind == kind,
                        )
                    )
                    session.flush()
                for payload in payloads:
                    record_id = payload[id_field]
                    row = session.get(
                        CurrentRecordRow,
                        {
                            "user_id": user_id,
                            "kind": kind,
                            "record_id": record_id,
                        },
                    )
                    if row is None:
                        session.add(
                            CurrentRecordRow(
                                user_id=user_id,
                                kind=kind,
                                record_id=record_id,
                                payload=payload,
                            )
                        )
                    else:
                        row.payload = payload
                        row.updated_at = utc_now()
        return {kind: len(payloads) for kind, (_, payloads) in prepared.items()}

    def replace_records(self, user_id: str, kind: str, records: Iterable[Mapping[str, Any]]) -> int:
        materialized = list(records)
        id_field = self._id_field(kind)
        payloads = [jsonable(item) for item in materialized]
        for payload in payloads:
            if not isinstance(payload.get(id_field), str) or not payload[id_field]:
                raise StorageError(f"{kind} record requires {id_field}")
        with self._session() as session:
            session.execute(
                delete(CurrentRecordRow).where(
                    CurrentRecordRow.user_id == user_id,
                    CurrentRecordRow.kind == kind,
                )
            )
            session.add_all(
                CurrentRecordRow(
                    user_id=user_id,
                    kind=kind,
                    record_id=payload[id_field],
                    payload=payload,
                )
                for payload in payloads
            )
        return len(payloads)

    def list_records(self, user_id: str, kind: str) -> list[dict[str, Any]]:
        self._id_field(kind)
        with self._session() as session:
            rows = session.scalars(
                select(CurrentRecordRow)
                .where(
                    CurrentRecordRow.user_id == user_id,
                    CurrentRecordRow.kind == kind,
                )
                .order_by(CurrentRecordRow.record_id)
            ).all()
            return [dict(row.payload) for row in rows]

    def get_record(self, user_id: str, kind: str, record_id: str) -> dict[str, Any]:
        self._id_field(kind)
        with self._session() as session:
            row = session.get(
                CurrentRecordRow,
                {"user_id": user_id, "kind": kind, "record_id": record_id},
            )
            if row is None:
                raise RecordNotFound(f"{kind}:{record_id} not found")
            return dict(row.payload)

    def patch_record(
        self,
        user_id: str,
        kind: str,
        record_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        id_field = self._id_field(kind)
        clean_changes = jsonable(changes)
        if id_field in clean_changes and clean_changes[id_field] != record_id:
            raise StorageConflict(f"{id_field} cannot be changed")
        with self._session() as session:
            row = session.get(
                CurrentRecordRow,
                {"user_id": user_id, "kind": kind, "record_id": record_id},
            )
            if row is None:
                raise RecordNotFound(f"{kind}:{record_id} not found")
            updated = {**row.payload, **clean_changes, id_field: record_id}
            row.payload = updated
            row.updated_at = utc_now()
            return dict(updated)

    def create_snapshot(
        self,
        user_id: str,
        *,
        as_of: datetime,
        payload: Mapping[str, Any] | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        snapshot_id = snapshot_id or f"snapshot-{uuid4()}"
        body = jsonable(payload) if payload is not None else self.current_state(user_id)
        body = {
            **body,
            "snapshot_id": snapshot_id,
            "user_id": user_id,
            "as_of": as_of.isoformat(),
            "timezone": body.get("timezone", "Asia/Seoul"),
        }
        try:
            with self._session() as session:
                session.add(
                    FinancialSnapshotRow(
                        snapshot_id=snapshot_id,
                        user_id=user_id,
                        as_of=as_of,
                        payload=body,
                    )
                )
        except IntegrityError as exc:
            raise StorageConflict(f"snapshot {snapshot_id} already exists") from exc
        return body

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(FinancialSnapshotRow, snapshot_id)
            if row is None:
                raise RecordNotFound(f"snapshot:{snapshot_id} not found")
            return dict(row.payload)

    def current_state(self, user_id: str) -> dict[str, Any]:
        records, _revision = self.current_records_with_revision(user_id)
        state = {
            kind: records[kind]
            for kind in CURRENT_KINDS
            if kind not in {"data_quality_notices", "preferences"}
        }
        notices = records["data_quality_notices"]
        state["data_quality"] = {
            "notices": notices,
            "missing_sources": [],
            "stale_sources": [],
            "unconfirmed_items": [
                item["candidate_id"]
                for item in state["candidates"]
                if item.get("status") == "PENDING"
            ],
        }
        state.pop("candidates")
        stored_preferences = records["preferences"]
        preference = stored_preferences[0] if stored_preferences else {}
        state["preferences"] = {
            "protection_level": preference.get("protection_level", 0.9),
            "minimum_total_reserve": preference.get("minimum_total_reserve", 0),
        }
        return state

    def current_records_with_revision(
        self, user_id: str
    ) -> tuple[dict[str, list[dict[str, Any]]], str]:
        """Read all mutable user state in one transaction and hash that exact view."""

        with self._session() as session:
            rows = session.scalars(
                select(CurrentRecordRow)
                .where(CurrentRecordRow.user_id == user_id)
                .order_by(CurrentRecordRow.kind, CurrentRecordRow.record_id)
            ).all()
            records = {kind: [] for kind in CURRENT_KINDS}
            canonical: list[dict[str, Any]] = []
            for row in rows:
                if row.kind not in records:
                    continue
                payload = dict(row.payload)
                records[row.kind].append(payload)
                canonical.append(
                    {
                        "kind": row.kind,
                        "record_id": row.record_id,
                        "payload": payload,
                    }
                )
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return records, hashlib.sha256(encoded).hexdigest()

    def current_state_revision(self, user_id: str) -> str:
        return self.current_records_with_revision(user_id)[1]

    def create_analysis(
        self,
        user_id: str,
        *,
        trigger_type: str,
        metadata: Mapping[str, Any] | None = None,
        analysis_id: str | None = None,
    ) -> dict[str, Any]:
        analysis_id = analysis_id or f"analysis-{uuid4()}"
        with self._session() as session:
            row = AnalysisRunRow(
                analysis_id=analysis_id,
                user_id=user_id,
                snapshot_id=None,
                status="QUEUED",
                trigger_type=trigger_type,
                metadata_json=jsonable(metadata or {}),
            )
            session.add(row)
            session.flush()
            session.add(
                AnalysisEventRow(
                    analysis_id=analysis_id,
                    sequence=1,
                    status="QUEUED",
                    message="분석 작업이 등록되었습니다.",
                    details={},
                )
            )
        return self.get_analysis(analysis_id)

    def transition_analysis(
        self,
        analysis_id: str,
        status: str,
        *,
        message: str,
        details: Mapping[str, Any] | None = None,
        snapshot_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in ANALYSIS_STATUSES:
            raise InvalidAnalysisTransition(f"unknown status {status}")
        with self._session() as session:
            row = session.get(AnalysisRunRow, analysis_id)
            if row is None:
                raise RecordNotFound(f"analysis:{analysis_id} not found")
            if status not in ANALYSIS_TRANSITIONS[row.status]:
                raise InvalidAnalysisTransition(f"{row.status} cannot transition to {status}")
            sequence = (
                session.scalar(
                    select(func.max(AnalysisEventRow.sequence)).where(
                        AnalysisEventRow.analysis_id == analysis_id
                    )
                )
                or 0
            ) + 1
            row.status = status
            row.updated_at = utc_now()
            if snapshot_id is not None:
                row.snapshot_id = snapshot_id
            if result is not None:
                row.result = jsonable(result)
            if error is not None:
                row.error = jsonable(error)
            if status in {"COMPLETED", "FAILED"}:
                row.completed_at = utc_now()
            session.add(
                AnalysisEventRow(
                    analysis_id=analysis_id,
                    sequence=sequence,
                    status=status,
                    message=message,
                    details=jsonable(details or {}),
                )
            )
        return self.get_analysis(analysis_id)

    def get_analysis(self, analysis_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(AnalysisRunRow, analysis_id)
            if row is None:
                raise RecordNotFound(f"analysis:{analysis_id} not found")
            return self._analysis_dict(row)

    def list_analysis_events(self, analysis_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            if session.get(AnalysisRunRow, analysis_id) is None:
                raise RecordNotFound(f"analysis:{analysis_id} not found")
            rows = session.scalars(
                select(AnalysisEventRow)
                .where(AnalysisEventRow.analysis_id == analysis_id)
                .order_by(AnalysisEventRow.sequence)
            ).all()
            return [
                {
                    "event_id": row.event_id,
                    "analysis_id": row.analysis_id,
                    "sequence": row.sequence,
                    "status": row.status,
                    "message": row.message,
                    "details": row.details,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def save_report(
        self,
        *,
        analysis_id: str,
        user_id: str,
        snapshot_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        report = jsonable(payload)
        with self._session() as session:
            analysis = session.get(AnalysisRunRow, analysis_id)
            if analysis is None:
                raise RecordNotFound(f"analysis:{analysis_id} not found")
            if analysis.user_id != user_id or analysis.snapshot_id != snapshot_id:
                raise StorageConflict("report does not match its analysis")
            if session.get(AnalysisReportRow, analysis_id) is not None:
                raise StorageConflict("analysis report is immutable")
            session.add(
                AnalysisReportRow(
                    analysis_id=analysis_id,
                    user_id=user_id,
                    snapshot_id=snapshot_id,
                    payload=report,
                )
            )
        return report

    def latest_report(self, user_id: str) -> dict[str, Any]:
        with self._session() as session:
            pointer = session.get(LatestReportPointerRow, user_id)
            if pointer is not None:
                row = session.get(AnalysisReportRow, pointer.analysis_id)
                if row is not None and row.user_id == user_id:
                    return dict(row.payload)
            row = session.scalar(
                select(AnalysisReportRow)
                .join(
                    AnalysisRunRow,
                    AnalysisRunRow.analysis_id == AnalysisReportRow.analysis_id,
                )
                .where(
                    AnalysisReportRow.user_id == user_id,
                    AnalysisRunRow.status == "COMPLETED",
                )
                .order_by(AnalysisReportRow.created_at.desc())
                .limit(1)
            )
            if row is None:
                raise RecordNotFound(f"latest report for user:{user_id} not found")
            return dict(row.payload)

    def promote_latest_report(self, *, user_id: str, analysis_id: str) -> dict[str, Any]:
        with self._session() as session:
            report = session.get(AnalysisReportRow, analysis_id)
            if report is None or report.user_id != user_id:
                raise RecordNotFound(f"analysis report:{analysis_id} not found")
            pointer = session.get(LatestReportPointerRow, user_id)
            if pointer is None:
                session.add(
                    LatestReportPointerRow(
                        user_id=user_id,
                        analysis_id=analysis_id,
                    )
                )
            else:
                pointer.analysis_id = analysis_id
                pointer.updated_at = utc_now()
        return self.latest_report(user_id)

    def latest_analysis(self, user_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.scalar(
                select(AnalysisRunRow)
                .where(AnalysisRunRow.user_id == user_id)
                .order_by(AnalysisRunRow.created_at.desc())
                .limit(1)
            )
            return self._analysis_dict(row) if row is not None else None

    def save_recommendation(
        self,
        *,
        analysis_id: str,
        user_id: str,
        payload: Mapping[str, Any],
        recommendation_id: str | None = None,
    ) -> dict[str, Any]:
        recommendation_id = recommendation_id or f"recommendation-{uuid4()}"
        body = {**jsonable(payload), "recommendation_id": recommendation_id}
        with self._session() as session:
            session.add(
                RecommendationRow(
                    recommendation_id=recommendation_id,
                    analysis_id=analysis_id,
                    user_id=user_id,
                    status="PENDING",
                    payload=body,
                )
            )
        return self.get_recommendation(user_id, recommendation_id)

    def list_recommendations(self, user_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.scalars(
                select(RecommendationRow)
                .where(RecommendationRow.user_id == user_id)
                .order_by(RecommendationRow.created_at.desc())
            ).all()
            return [self._recommendation_dict(row) for row in rows]

    def get_recommendation(self, user_id: str, recommendation_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(RecommendationRow, recommendation_id)
            if row is None or row.user_id != user_id:
                raise RecordNotFound(f"recommendation:{recommendation_id} not found")
            return self._recommendation_dict(row)

    def record_recommendation_decision(
        self,
        user_id: str,
        recommendation_id: str,
        *,
        decision: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        allowed = {"APPROVED", "REJECTED", "ALTERNATIVES_REQUESTED"}
        if decision not in allowed:
            raise StorageError(f"unsupported decision {decision}")
        with self._session() as session:
            row = session.get(RecommendationRow, recommendation_id)
            if row is None or row.user_id != user_id:
                raise RecordNotFound(f"recommendation:{recommendation_id} not found")
            if decision in {"APPROVED", "REJECTED"} and row.status != "PENDING":
                raise StorageConflict("recommendation already has a final decision")
            if decision in {"APPROVED", "REJECTED"}:
                row.status = decision
                row.updated_at = utc_now()
            session.add(
                ApprovalAuditRow(
                    audit_id=f"audit-{uuid4()}",
                    recommendation_id=recommendation_id,
                    analysis_id=row.analysis_id,
                    user_id=user_id,
                    decision=decision,
                    effect="VIRTUAL_ONLY",
                    details=jsonable(details or {}),
                )
            )
        return self.get_recommendation(user_id, recommendation_id)

    def list_approval_audit(self, user_id: str, recommendation_id: str) -> list[dict[str, Any]]:
        with self._session() as session:
            rows = session.scalars(
                select(ApprovalAuditRow)
                .where(
                    ApprovalAuditRow.user_id == user_id,
                    ApprovalAuditRow.recommendation_id == recommendation_id,
                )
                .order_by(ApprovalAuditRow.created_at)
            ).all()
            return [
                {
                    "audit_id": row.audit_id,
                    "recommendation_id": row.recommendation_id,
                    "analysis_id": row.analysis_id,
                    "decision": row.decision,
                    "effect": row.effect,
                    "details": row.details,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def record_tool_execution(
        self,
        *,
        analysis_id: str,
        agent_run_id: str,
        sequence: int,
        tool_name: str,
        input_payload: Mapping[str, Any],
        output_payload: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        execution_id = f"tool-{uuid4()}"
        with self._session() as session:
            session.add(
                ToolExecutionRow(
                    tool_execution_id=execution_id,
                    analysis_id=analysis_id,
                    agent_run_id=agent_run_id,
                    sequence=sequence,
                    tool_name=tool_name,
                    input_payload=jsonable(input_payload),
                    output_payload=jsonable(output_payload),
                    error=jsonable(error),
                )
            )
        return {
            "tool_execution_id": execution_id,
            "analysis_id": analysis_id,
            "agent_run_id": agent_run_id,
            "sequence": sequence,
            "tool_name": tool_name,
            "input": jsonable(input_payload),
            "output": jsonable(output_payload),
            "error": jsonable(error),
        }

    def list_tool_executions(
        self, analysis_id: str, agent_run_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._session() as session:
            statement = select(ToolExecutionRow).where(ToolExecutionRow.analysis_id == analysis_id)
            if agent_run_id is not None:
                statement = statement.where(ToolExecutionRow.agent_run_id == agent_run_id)
            rows = session.scalars(statement.order_by(ToolExecutionRow.sequence)).all()
            return [
                {
                    "tool_execution_id": row.tool_execution_id,
                    "analysis_id": row.analysis_id,
                    "agent_run_id": row.agent_run_id,
                    "sequence": row.sequence,
                    "tool_name": row.tool_name,
                    "input": row.input_payload,
                    "output": row.output_payload,
                    "error": row.error,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def begin_ai_interpretation(
        self,
        *,
        analysis_id: str,
        user_id: str,
        snapshot_revision: str,
        contract_version: str,
        prompt_version: str,
        correlation_id: str | None,
        model_name: str,
        request_payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        ai_request_id = f"ai-{uuid4()}"
        idempotency_key = (
            f"{analysis_id}:{snapshot_revision}:{contract_version}:{prompt_version}"
        )
        try:
            with self._session() as session:
                session.add(
                    AIInterpretationRow(
                        ai_request_id=ai_request_id,
                        analysis_id=analysis_id,
                        user_id=user_id,
                        snapshot_revision=snapshot_revision,
                        contract_version=contract_version,
                        prompt_version=prompt_version,
                        idempotency_key=idempotency_key,
                        status="RUNNING",
                        correlation_id=correlation_id,
                        model_name=model_name,
                        request_payload=jsonable(request_payload),
                        attempt_count=0,
                    )
                )
        except IntegrityError:
            existing = self.mark_ai_interpretation_reused(
                analysis_id=analysis_id,
                snapshot_revision=snapshot_revision,
                contract_version=contract_version,
                prompt_version=prompt_version,
            )
            if existing is None:
                raise StorageConflict("AI interpretation request already exists") from None
            return existing, False
        return self.get_ai_interpretation(ai_request_id), True

    def get_ai_interpretation(self, ai_request_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(AIInterpretationRow, ai_request_id)
            if row is None:
                raise RecordNotFound(f"ai_interpretation:{ai_request_id} not found")
            return self._ai_interpretation_dict(row)

    def get_ai_interpretation_by_key(
        self,
        *,
        analysis_id: str,
        snapshot_revision: str,
        contract_version: str,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.scalar(
                select(AIInterpretationRow).where(
                    AIInterpretationRow.analysis_id == analysis_id,
                    AIInterpretationRow.snapshot_revision == snapshot_revision,
                    AIInterpretationRow.contract_version == contract_version,
                    AIInterpretationRow.prompt_version == prompt_version,
                )
            )
            return self._ai_interpretation_dict(row) if row is not None else None

    def mark_ai_interpretation_reused(
        self,
        *,
        analysis_id: str,
        snapshot_revision: str,
        contract_version: str,
        prompt_version: str,
    ) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.scalar(
                select(AIInterpretationRow).where(
                    AIInterpretationRow.analysis_id == analysis_id,
                    AIInterpretationRow.snapshot_revision == snapshot_revision,
                    AIInterpretationRow.contract_version == contract_version,
                    AIInterpretationRow.prompt_version == prompt_version,
                )
            )
            if row is None:
                return None
            row.reuse_count += 1
            row.updated_at = utc_now()
        return self.get_ai_interpretation_by_key(
            analysis_id=analysis_id,
            snapshot_revision=snapshot_revision,
            contract_version=contract_version,
            prompt_version=prompt_version,
        )

    def finalize_ai_interpretation(
        self,
        ai_request_id: str,
        *,
        status: str,
        response_payload: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        attempt_count: int | None = None,
    ) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(AIInterpretationRow, ai_request_id)
            if row is None:
                raise RecordNotFound(f"ai_interpretation:{ai_request_id} not found")
            row.status = status
            row.response_payload = (
                jsonable(response_payload) if response_payload is not None else None
            )
            row.error = jsonable(error) if error is not None else None
            row.updated_at = utc_now()
            if attempt_count is not None:
                row.attempt_count = attempt_count
            if status in {"SUCCEEDED", "FAILED", "FALLBACK"}:
                row.completed_at = utc_now()
        return self.get_ai_interpretation(ai_request_id)

    def ai_interpretation_metrics(
        self,
        user_id: str,
        *,
        analysis_id: str | None = None,
    ) -> dict[str, Any]:
        with self._session() as session:
            statement = select(AIInterpretationRow).where(AIInterpretationRow.user_id == user_id)
            if analysis_id is not None:
                statement = statement.where(AIInterpretationRow.analysis_id == analysis_id)
            rows = session.scalars(statement).all()
        status_counts = {
            "RUNNING": 0,
            "SUCCEEDED": 0,
            "FAILED": 0,
            "FALLBACK": 0,
        }
        attempt_sum = 0
        max_attempts = 0
        duplicate_prevented_count = 0
        for row in rows:
            status_counts[row.status] = status_counts.get(row.status, 0) + 1
            attempt_sum += row.attempt_count
            max_attempts = max(max_attempts, row.attempt_count)
            duplicate_prevented_count += row.reuse_count
        total_requests = len(rows)
        return {
            "total_requests": total_requests,
            "running": status_counts.get("RUNNING", 0),
            "succeeded": status_counts.get("SUCCEEDED", 0),
            "failed": status_counts.get("FAILED", 0),
            "fallback": status_counts.get("FALLBACK", 0),
            "duplicate_prevented_count": duplicate_prevented_count,
            "average_attempt_count": (
                round(attempt_sum / total_requests, 2) if total_requests else 0.0
            ),
            "max_attempt_count": max_attempts,
        }

    @staticmethod
    def _id_field(kind: str) -> str:
        try:
            return CURRENT_KINDS[kind]
        except KeyError as exc:
            raise StorageError(f"unsupported current record kind {kind}") from exc

    @staticmethod
    def _analysis_dict(row: AnalysisRunRow) -> dict[str, Any]:
        return {
            "analysis_id": row.analysis_id,
            "user_id": row.user_id,
            "snapshot_id": row.snapshot_id,
            "status": row.status,
            "trigger_type": row.trigger_type,
            "metadata": row.metadata_json,
            "result": row.result,
            "error": row.error,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }

    @staticmethod
    def _recommendation_dict(row: RecommendationRow) -> dict[str, Any]:
        return {
            **row.payload,
            "recommendation_id": row.recommendation_id,
            "analysis_id": row.analysis_id,
            "status": row.status,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }

    @staticmethod
    def _ai_interpretation_dict(row: AIInterpretationRow) -> dict[str, Any]:
        return {
            "ai_request_id": row.ai_request_id,
            "analysis_id": row.analysis_id,
            "user_id": row.user_id,
            "snapshot_revision": row.snapshot_revision,
            "contract_version": row.contract_version,
            "prompt_version": row.prompt_version,
            "idempotency_key": row.idempotency_key,
            "status": row.status,
            "correlation_id": row.correlation_id,
            "model_name": row.model_name,
            "request_payload": row.request_payload,
            "response_payload": row.response_payload,
            "error": row.error,
            "attempt_count": row.attempt_count,
            "reuse_count": row.reuse_count,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }


SQLAlchemyRepository = FlowGuardRepository

__all__ = [
    "ANALYSIS_STATUSES",
    "Base",
    "FlowGuardRepository",
    "InvalidAnalysisTransition",
    "RecordNotFound",
    "SQLAlchemyRepository",
    "StorageConflict",
    "StorageError",
    "jsonable",
]
