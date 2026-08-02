"""SQLAlchemy persistence for current data, immutable analysis inputs, and audit state."""

from __future__ import annotations

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
    Boolean,
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
    update,
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
    "INTERPRETATION_REQUESTING",
    "INTERPRETATION_VALIDATING",
    "COMPLETED",
    "FAILED",
)

ANALYSIS_TRANSITIONS = {
    "QUEUED": {"SNAPSHOT_BUILDING", "FAILED"},
    "SNAPSHOT_BUILDING": {"BASELINE_ANALYZING", "FAILED"},
    "BASELINE_ANALYZING": {"AGENT_INVESTIGATING", "FAILED"},
    "AGENT_INVESTIGATING": {"PLAN_EVALUATING", "FAILED"},
    "PLAN_EVALUATING": {"REPORT_BUILDING", "FAILED"},
    "REPORT_BUILDING": {"INTERPRETATION_REQUESTING", "COMPLETED", "FAILED"},
    "INTERPRETATION_REQUESTING": {"INTERPRETATION_VALIDATING", "COMPLETED", "FAILED"},
    "INTERPRETATION_VALIDATING": {"COMPLETED", "FAILED"},
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


class DataRevisionConflict(StorageConflict):
    """A current-state write was prepared from a stale data revision."""


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


class DataRevisionRow(Base):
    __tablename__ = "data_revisions"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


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
    snapshot_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class AIInterpretationRunRow(Base):
    __tablename__ = "ai_interpretation_runs"

    interpretation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_runs.analysis_id"), nullable=False, index=True
    )
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("financial_snapshots.snapshot_id"), nullable=False, index=True
    )
    snapshot_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIClassificationRunRow(Base):
    __tablename__ = "ai_classification_runs"

    classification_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    import_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    label_set_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_count: Mapped[int] = mapped_column(Integer, nullable=False)
    group_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    merged_label_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
        self._backfill_latest_report_pointers()

    @contextmanager
    def _session(self) -> Iterable[Session]:
        with self._session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def _backfill_latest_report_pointers(self) -> None:
        """Give databases created before pointer support an initial latest report."""

        with self._session() as session:
            user_ids = session.scalars(select(AnalysisReportRow.user_id).distinct()).all()
            for user_id in user_ids:
                if session.get(LatestReportPointerRow, user_id) is not None:
                    continue
                report = session.scalar(
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
                if report is None:
                    continue
                revision = str(
                    report.payload.get("current_state_revision")
                    or report.payload.get("analysis_revision")
                    or "rev-0"
                )
                session.add(
                    LatestReportPointerRow(
                        user_id=user_id,
                        analysis_id=report.analysis_id,
                        snapshot_revision=revision,
                    )
                )

    @staticmethod
    def _data_revision(session: Session, user_id: str) -> int:
        row = session.get(DataRevisionRow, user_id)
        return row.revision if row is not None else 0

    @staticmethod
    def _bump_data_revision(session: Session, user_id: str) -> int:
        dialect = session.get_bind().dialect.name
        if dialect in {"postgresql", "sqlite"}:
            now = utc_now()
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert

            statement = (
                insert(DataRevisionRow)
                .values(user_id=user_id, revision=1, updated_at=now)
                .on_conflict_do_update(
                    index_elements=[DataRevisionRow.user_id],
                    set_={
                        "revision": DataRevisionRow.revision + 1,
                        "updated_at": now,
                    },
                )
                .returning(DataRevisionRow.revision)
            )
            revision = session.scalar(statement)
            if revision is None:
                raise StorageError("data revision increment returned no value")
            return revision

        row = session.get(DataRevisionRow, user_id, with_for_update=True)
        if row is None:
            row = DataRevisionRow(user_id=user_id, revision=1)
            session.add(row)
        else:
            row.revision += 1
            row.updated_at = utc_now()
        return row.revision

    def upsert_records(self, user_id: str, kind: str, records: Iterable[Mapping[str, Any]]) -> int:
        id_field = self._id_field(kind)
        materialized = [jsonable(item) for item in records]
        count = 0
        with self._session() as session:
            if materialized:
                self._bump_data_revision(session, user_id)
            for payload in materialized:
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
        classification_completion: Mapping[str, Any] | None = None,
        candidate_preconditions: Mapping[str, Mapping[str, Any]] | None = None,
        expected_revision: str | None = None,
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
            if prepared:
                new_revision = self._bump_data_revision(session, user_id)
                if expected_revision is not None and expected_revision != f"rev-{new_revision - 1}":
                    raise DataRevisionConflict(
                        "data revision changed before the record bundle was saved"
                    )
            for candidate_id, expected in (candidate_preconditions or {}).items():
                candidate = session.get(
                    CurrentRecordRow,
                    {
                        "user_id": user_id,
                        "kind": "candidates",
                        "record_id": candidate_id,
                    },
                    with_for_update=True,
                )
                if candidate is None or candidate.payload != jsonable(expected):
                    raise StorageConflict(
                        f"candidate:{candidate_id} changed before the decision was saved"
                    )
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
            if classification_completion is not None:
                classification_id = str(classification_completion["classification_id"])
                classification = session.get(
                    AIClassificationRunRow,
                    classification_id,
                    with_for_update=True,
                )
                if classification is None:
                    raise RecordNotFound(f"classification:{classification_id} not found")
                if classification.user_id != user_id:
                    raise StorageConflict("classification run belongs to another user")
                if classification.import_id != classification_completion.get("import_id"):
                    raise StorageConflict("classification import identity mismatch")
                if classification.label_set_hash != classification_completion.get("label_set_hash"):
                    raise StorageConflict("classification label-set identity mismatch")
                if (
                    classification_completion.get("status") == "SUCCEEDED"
                    and classification_completion.get("applied") is True
                ):
                    self._complete_classification_success(
                        session,
                        classification,
                        classification_completion,
                    )
                else:
                    self._verify_classification_persistence_guard(
                        classification,
                        classification_completion,
                    )
        return {kind: len(payloads) for kind, (_, payloads) in prepared.items()}

    def split_candidate_group(
        self,
        user_id: str,
        candidate_id: str,
        *,
        alternatives: Iterable[Mapping[str, Any]],
        notices: Iterable[Mapping[str, Any]],
        expected_candidate: Mapping[str, Any] | None = None,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        """Atomically supersede one pending AI group with deterministic alternatives."""

        prepared_alternatives = [jsonable(item) for item in alternatives]
        prepared_notices = [jsonable(item) for item in notices]
        for payload in prepared_alternatives:
            if not isinstance(payload.get("candidate_id"), str) or not payload["candidate_id"]:
                raise StorageError("candidates record requires candidate_id")
        for payload in prepared_notices:
            if not isinstance(payload.get("notice_id"), str) or not payload["notice_id"]:
                raise StorageError("data_quality_notices record requires notice_id")

        with self._session() as session:
            new_revision = self._bump_data_revision(session, user_id)
            if expected_revision is not None and expected_revision != f"rev-{new_revision - 1}":
                raise StorageConflict("data revision changed before candidate split")
            source = session.get(
                CurrentRecordRow,
                {
                    "user_id": user_id,
                    "kind": "candidates",
                    "record_id": candidate_id,
                },
                with_for_update=True,
            )
            if source is None:
                raise RecordNotFound(f"candidate:{candidate_id} not found")
            source_payload = dict(source.payload)
            if expected_candidate is not None and source_payload != jsonable(expected_candidate):
                raise StorageConflict("candidate changed before group split")
            classification = source_payload.get("classification_group")
            if (
                source_payload.get("status") != "PENDING"
                or not isinstance(classification, Mapping)
                or classification.get("can_split") is not True
                or classification.get("user_split") is True
            ):
                raise StorageConflict("candidate group is no longer splittable")

            for alternative in prepared_alternatives:
                alternative_id = str(alternative["candidate_id"])
                existing = session.get(
                    CurrentRecordRow,
                    {
                        "user_id": user_id,
                        "kind": "candidates",
                        "record_id": alternative_id,
                    },
                    with_for_update=True,
                )
                if existing is not None and existing.payload.get("status") != "PENDING":
                    raise StorageConflict(
                        f"candidate:{alternative_id} already has a final decision"
                    )

            source.payload = {
                **source_payload,
                "status": "REJECTED",
                "classification_group": {
                    **dict(classification),
                    "can_split": False,
                    "user_split": True,
                },
            }
            source.updated_at = utc_now()
            for alternative in prepared_alternatives:
                alternative_id = str(alternative["candidate_id"])
                row = session.get(
                    CurrentRecordRow,
                    {
                        "user_id": user_id,
                        "kind": "candidates",
                        "record_id": alternative_id,
                    },
                )
                if row is None:
                    session.add(
                        CurrentRecordRow(
                            user_id=user_id,
                            kind="candidates",
                            record_id=alternative_id,
                            payload=alternative,
                        )
                    )
                else:
                    row.payload = alternative
                    row.updated_at = utc_now()

            confirmation_notices = session.scalars(
                select(CurrentRecordRow).where(
                    CurrentRecordRow.user_id == user_id,
                    CurrentRecordRow.kind == "data_quality_notices",
                )
            ).all()
            for row in confirmation_notices:
                if row.payload.get(
                    "code"
                ) == "USER_CONFIRMATION_REQUIRED" and candidate_id in row.payload.get(
                    "record_ids", []
                ):
                    session.delete(row)
            for notice in prepared_notices:
                notice_id = str(notice["notice_id"])
                row = session.get(
                    CurrentRecordRow,
                    {
                        "user_id": user_id,
                        "kind": "data_quality_notices",
                        "record_id": notice_id,
                    },
                )
                if row is None:
                    session.add(
                        CurrentRecordRow(
                            user_id=user_id,
                            kind="data_quality_notices",
                            record_id=notice_id,
                            payload=notice,
                        )
                    )
                else:
                    row.payload = notice
                    row.updated_at = utc_now()

        return {
            "candidate_id": candidate_id,
            "candidates": prepared_alternatives,
        }

    def replace_records(self, user_id: str, kind: str, records: Iterable[Mapping[str, Any]]) -> int:
        materialized = list(records)
        id_field = self._id_field(kind)
        payloads = [jsonable(item) for item in materialized]
        for payload in payloads:
            if not isinstance(payload.get(id_field), str) or not payload[id_field]:
                raise StorageError(f"{kind} record requires {id_field}")
        with self._session() as session:
            self._bump_data_revision(session, user_id)
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

    def reset_user_data(self, user_id: str) -> None:
        """Delete one user's demo state and analysis artifacts atomically."""

        with self._session() as session:
            # Keep the sequencing row so a later import cannot reuse an older
            # revision identifier after reset (the A -> reset -> A ABA case).
            self._bump_data_revision(session, user_id)
            analysis_ids = list(
                session.scalars(
                    select(AnalysisRunRow.analysis_id).where(AnalysisRunRow.user_id == user_id)
                )
            )
            session.execute(
                delete(AIClassificationRunRow).where(AIClassificationRunRow.user_id == user_id)
            )
            session.execute(delete(ApprovalAuditRow).where(ApprovalAuditRow.user_id == user_id))
            session.execute(delete(RecommendationRow).where(RecommendationRow.user_id == user_id))
            session.execute(
                delete(LatestReportPointerRow).where(LatestReportPointerRow.user_id == user_id)
            )
            session.execute(delete(AnalysisReportRow).where(AnalysisReportRow.user_id == user_id))
            if analysis_ids:
                session.execute(
                    delete(AIInterpretationRunRow).where(
                        AIInterpretationRunRow.analysis_id.in_(analysis_ids)
                    )
                )
                session.execute(
                    delete(ToolExecutionRow).where(ToolExecutionRow.analysis_id.in_(analysis_ids))
                )
                session.execute(
                    delete(AnalysisEventRow).where(AnalysisEventRow.analysis_id.in_(analysis_ids))
                )
                session.execute(
                    delete(AnalysisRunRow).where(AnalysisRunRow.analysis_id.in_(analysis_ids))
                )
            session.execute(
                delete(FinancialSnapshotRow).where(FinancialSnapshotRow.user_id == user_id)
            )
            session.execute(delete(CurrentRecordRow).where(CurrentRecordRow.user_id == user_id))

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
            self._bump_data_revision(session, user_id)
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
        """Read all mutable user state with its monotonic revision."""

        with self._session() as session:
            revision_row = session.get(DataRevisionRow, user_id, with_for_update=True)
            rows = session.scalars(
                select(CurrentRecordRow)
                .where(CurrentRecordRow.user_id == user_id)
                .order_by(CurrentRecordRow.kind, CurrentRecordRow.record_id)
            ).all()
            records = {kind: [] for kind in CURRENT_KINDS}
            for row in rows:
                if row.kind not in records:
                    continue
                payload = dict(row.payload)
                records[row.kind].append(payload)
            revision = revision_row.revision if revision_row is not None else 0
        return records, f"rev-{revision}"

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
            if pointer is None:
                raise RecordNotFound(f"latest report for user:{user_id} not found")
            row = session.get(AnalysisReportRow, pointer.analysis_id)
            if row is None:
                raise RecordNotFound(f"latest report for user:{user_id} not found")
            return dict(row.payload)

    def report_for_analysis(self, analysis_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(AnalysisReportRow, analysis_id)
            if row is None:
                raise RecordNotFound(f"report for analysis:{analysis_id} not found")
            return dict(row.payload)

    def promote_report_if_current(
        self,
        *,
        user_id: str,
        analysis_id: str,
        snapshot_revision: str,
    ) -> bool:
        """Atomically move the latest pointer only for the current data revision."""

        with self._session() as session:
            revision_row = session.get(DataRevisionRow, user_id, with_for_update=True)
            current_revision = f"rev-{revision_row.revision if revision_row else 0}"
            if snapshot_revision != current_revision:
                return False
            analysis = session.get(AnalysisRunRow, analysis_id)
            report = session.get(AnalysisReportRow, analysis_id)
            if (
                analysis is None
                or report is None
                or analysis.user_id != user_id
                or report.user_id != user_id
                or analysis.status != "COMPLETED"
            ):
                raise StorageConflict("only a completed matching report can be promoted")
            pointer = session.get(LatestReportPointerRow, user_id)
            if pointer is None:
                session.add(
                    LatestReportPointerRow(
                        user_id=user_id,
                        analysis_id=analysis_id,
                        snapshot_revision=snapshot_revision,
                    )
                )
            else:
                pointer.analysis_id = analysis_id
                pointer.snapshot_revision = snapshot_revision
                pointer.updated_at = utc_now()
            return True

    def complete_analysis_report(
        self,
        *,
        user_id: str,
        analysis_id: str,
        snapshot_revision: str,
        message: str,
        result: Mapping[str, Any],
        interpretation_id: str | None = None,
    ) -> dict[str, Any]:
        """Complete a run and conditionally promote its immutable report atomically."""

        with self._session() as session:
            revision_row = session.get(DataRevisionRow, user_id, with_for_update=True)
            latest_data_revision = f"rev-{revision_row.revision if revision_row else 0}"
            analysis = session.get(AnalysisRunRow, analysis_id)
            report = session.get(AnalysisReportRow, analysis_id)
            if (
                analysis is None
                or report is None
                or analysis.user_id != user_id
                or report.user_id != user_id
                or analysis.snapshot_id != report.snapshot_id
                or analysis.status
                not in {
                    "REPORT_BUILDING",
                    "INTERPRETATION_REQUESTING",
                    "INTERPRETATION_VALIDATING",
                }
            ):
                raise StorageConflict("only a finalizing matching report can be completed")

            promoted = snapshot_revision == latest_data_revision
            interpretation_status = str(result.get("interpretation_status", "NOT_REQUESTED"))
            if not promoted and interpretation_id is not None:
                interpretation = session.get(AIInterpretationRunRow, interpretation_id)
                if interpretation is None or interpretation.analysis_id != analysis_id:
                    raise StorageConflict("interpretation run does not match its analysis")
                interpretation.status = "STALE"
                interpretation.completed_at = interpretation.completed_at or utc_now()
                interpretation_status = "STALE"

            completed_result = {
                **jsonable(result),
                "report_available": promoted,
                "analysis_status": "SUCCEEDED" if promoted else "SUPERSEDED",
                "interpretation_status": interpretation_status,
                "execution_stage": "COMPLETED",
                "snapshot_revision": snapshot_revision,
                "latest_data_revision": latest_data_revision,
                "interpretation_id": interpretation_id,
            }
            sequence = (
                session.scalar(
                    select(func.max(AnalysisEventRow.sequence)).where(
                        AnalysisEventRow.analysis_id == analysis_id
                    )
                )
                or 0
            ) + 1
            completed_at = utc_now()
            analysis.status = "COMPLETED"
            analysis.result = completed_result
            analysis.updated_at = completed_at
            analysis.completed_at = completed_at
            session.add(
                AnalysisEventRow(
                    analysis_id=analysis_id,
                    sequence=sequence,
                    status="COMPLETED",
                    message=message,
                    details={},
                )
            )

            if promoted:
                pointer = session.get(LatestReportPointerRow, user_id)
                if pointer is None:
                    session.add(
                        LatestReportPointerRow(
                            user_id=user_id,
                            analysis_id=analysis_id,
                            snapshot_revision=snapshot_revision,
                        )
                    )
                else:
                    pointer.analysis_id = analysis_id
                    pointer.snapshot_revision = snapshot_revision
                    pointer.updated_at = completed_at
            session.flush()
            return self._analysis_dict(analysis)

    def latest_report_pointer(self, user_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.get(LatestReportPointerRow, user_id)
            if row is None:
                return None
            return {
                "user_id": row.user_id,
                "analysis_id": row.analysis_id,
                "snapshot_revision": row.snapshot_revision,
                "updated_at": row.updated_at.isoformat(),
            }

    def latest_analysis(self, user_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.scalar(
                select(AnalysisRunRow)
                .where(AnalysisRunRow.user_id == user_id)
                .order_by(AnalysisRunRow.created_at.desc())
                .limit(1)
            )
            return self._analysis_dict(row) if row is not None else None

    def create_or_get_interpretation_run(
        self,
        *,
        analysis_id: str,
        snapshot_id: str,
        snapshot_revision: str,
        request_id: str,
        idempotency_key: str,
        contract_version: str,
        prompt_version: str,
        model_name: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        identity = {
            "analysis_id": analysis_id,
            "snapshot_id": snapshot_id,
            "snapshot_revision": snapshot_revision,
            "request_id": request_id,
            "contract_version": contract_version,
            "prompt_version": prompt_version,
        }
        try:
            with self._session() as session:
                existing = session.scalar(
                    select(AIInterpretationRunRow).where(
                        AIInterpretationRunRow.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    self._assert_interpretation_identity(existing, identity)
                    return self._interpretation_dict(existing), False
                row = AIInterpretationRunRow(
                    interpretation_id=f"interpretation-{uuid4()}",
                    analysis_id=analysis_id,
                    snapshot_id=snapshot_id,
                    snapshot_revision=snapshot_revision,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    contract_version=contract_version,
                    prompt_version=prompt_version,
                    model_name=model_name,
                    status="QUEUED",
                )
                session.add(row)
                session.flush()
                return self._interpretation_dict(row), True
        except IntegrityError as exc:
            # A concurrent insert may win after the initial lookup. Resolve the
            # unique-key race by returning that exact logical request only.
            with self._session() as session:
                existing = session.scalar(
                    select(AIInterpretationRunRow).where(
                        AIInterpretationRunRow.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise StorageConflict(
                        "could not resolve interpretation idempotency race"
                    ) from exc
                self._assert_interpretation_identity(existing, identity)
                return self._interpretation_dict(existing), False

    def update_interpretation_run(
        self,
        interpretation_id: str,
        *,
        status: str,
        attempt_count: int | None = None,
        fallback_used: bool | None = None,
        latency_ms: int | None = None,
        response_payload: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        allowed = {"QUEUED", "RUNNING", "SUCCEEDED", "FALLBACK", "FAILED", "STALE"}
        if status not in allowed:
            raise StorageError(f"unsupported interpretation status {status}")
        with self._session() as session:
            row = session.get(AIInterpretationRunRow, interpretation_id)
            if row is None:
                raise RecordNotFound(f"interpretation:{interpretation_id} not found")
            row.status = status
            if attempt_count is not None:
                row.attempt_count = attempt_count
            if fallback_used is not None:
                row.fallback_used = fallback_used
            if latency_ms is not None:
                row.latency_ms = latency_ms
            if response_payload is not None:
                row.response_payload = jsonable(response_payload)
            row.error_code = error_code
            if status in {"SUCCEEDED", "FALLBACK", "FAILED", "STALE"}:
                row.completed_at = row.completed_at or utc_now()
            session.flush()
            return self._interpretation_dict(row)

    def get_interpretation_run(self, interpretation_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(AIInterpretationRunRow, interpretation_id)
            if row is None:
                raise RecordNotFound(f"interpretation:{interpretation_id} not found")
            return self._interpretation_dict(row)

    def latest_interpretation_run(self, analysis_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            row = session.scalar(
                select(AIInterpretationRunRow)
                .where(AIInterpretationRunRow.analysis_id == analysis_id)
                .order_by(AIInterpretationRunRow.created_at.desc())
                .limit(1)
            )
            return self._interpretation_dict(row) if row is not None else None

    def create_or_get_classification_run(
        self,
        *,
        user_id: str,
        import_id: str,
        request_id: str,
        idempotency_key: str,
        label_set_hash: str,
        schema_version: str,
        contract_version: str,
        prompt_version: str,
        mode: str,
        label_count: int,
        model_name: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if mode not in {"SHADOW", "ON"}:
            raise StorageError(f"unsupported classification mode {mode}")
        if label_count < 0:
            raise StorageError("classification label_count cannot be negative")
        identity: dict[str, Any] = {
            "user_id": user_id,
            "import_id": import_id,
            "request_id": request_id,
            "label_set_hash": label_set_hash,
            "schema_version": schema_version,
            "contract_version": contract_version,
            "prompt_version": prompt_version,
            "mode": mode,
            "label_count": label_count,
        }
        try:
            with self._session() as session:
                existing = session.scalar(
                    select(AIClassificationRunRow).where(
                        AIClassificationRunRow.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    self._assert_classification_identity(existing, identity)
                    return self._classification_dict(existing), False
                row = AIClassificationRunRow(
                    classification_id=f"classification-{uuid4()}",
                    user_id=user_id,
                    import_id=import_id,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    label_set_hash=label_set_hash,
                    schema_version=schema_version,
                    contract_version=contract_version,
                    prompt_version=prompt_version,
                    mode=mode,
                    status="IN_PROGRESS",
                    label_count=label_count,
                    model_name=model_name,
                )
                session.add(row)
                session.flush()
                return self._classification_dict(row), True
        except IntegrityError as exc:
            with self._session() as session:
                existing = session.scalar(
                    select(AIClassificationRunRow).where(
                        AIClassificationRunRow.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise StorageConflict(
                        "could not resolve classification idempotency race"
                    ) from exc
                self._assert_classification_identity(existing, identity)
                return self._classification_dict(existing), False

    def update_classification_run(
        self,
        classification_id: str,
        *,
        status: str,
        applied: bool | None = None,
        attempt_count: int | None = None,
        fallback_used: bool | None = None,
        latency_ms: int | None = None,
        group_count: int | None = None,
        merged_label_count: int | None = None,
        response_payload: Mapping[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        terminal_statuses = {"SUCCEEDED", "REJECTED", "FAILED"}
        if status not in {"IN_PROGRESS", *terminal_statuses}:
            raise StorageError(f"unsupported classification status {status}")
        for field, value in {
            "attempt_count": attempt_count,
            "latency_ms": latency_ms,
            "group_count": group_count,
            "merged_label_count": merged_label_count,
        }.items():
            if value is not None and value < 0:
                raise StorageError(f"classification {field} cannot be negative")
        if response_payload is not None and status != "SUCCEEDED":
            raise StorageError("classification response payload requires SUCCEEDED status")
        if status == "SUCCEEDED" and (
            response_payload is None
            or group_count is None
            or merged_label_count is None
            or error_code is not None
        ):
            raise StorageError(
                "successful classification requires payload and complete group counts"
            )
        if status in {"REJECTED", "FAILED"} and (
            applied is True
            or response_payload is not None
            or group_count is not None
            or merged_label_count is not None
        ):
            raise StorageError("failed classification cannot contain an applied result")

        with self._session() as session:
            row = session.get(
                AIClassificationRunRow,
                classification_id,
                with_for_update=True,
            )
            if row is None:
                raise RecordNotFound(f"classification:{classification_id} not found")
            if status == "SUCCEEDED":
                self._validate_classification_success(
                    row,
                    group_count=group_count,
                    merged_label_count=merged_label_count,
                    response_payload=response_payload,
                )
            if row.status in terminal_statuses:
                self._assert_idempotent_classification_update(
                    row,
                    status=status,
                    applied=applied,
                    fallback_used=fallback_used,
                    group_count=group_count,
                    merged_label_count=merged_label_count,
                    response_payload=response_payload,
                    error_code=error_code,
                )
                return self._classification_dict(row)
            if status == "SUCCEEDED" and row.mode == "ON":
                raise StorageError(
                    "successful ON classification must be completed with apply_record_bundle"
                )
            applied_value = row.applied if applied is None else applied
            if applied_value and (status != "SUCCEEDED" or row.mode != "ON"):
                raise StorageError("only a successful ON classification can be applied")
            values: dict[str, Any] = {
                "status": status,
                "applied": applied_value,
                "error_code": error_code,
            }
            if attempt_count is not None:
                values["attempt_count"] = attempt_count
            if fallback_used is not None:
                values["fallback_used"] = fallback_used
            if latency_ms is not None:
                values["latency_ms"] = latency_ms
            if group_count is not None:
                values["group_count"] = group_count
            if merged_label_count is not None:
                values["merged_label_count"] = merged_label_count
            if response_payload is not None:
                values["response_payload"] = jsonable(response_payload)
            if status in terminal_statuses:
                values["completed_at"] = utc_now()
            transitioned = session.execute(
                update(AIClassificationRunRow)
                .where(
                    AIClassificationRunRow.classification_id == classification_id,
                    AIClassificationRunRow.status == "IN_PROGRESS",
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if transitioned.rowcount != 1:
                raise StorageConflict("classification terminal transition lost a concurrent race")
            session.expire(row)
            return self._classification_dict(row)

    def get_classification_run(self, classification_id: str) -> dict[str, Any]:
        with self._session() as session:
            row = session.get(AIClassificationRunRow, classification_id)
            if row is None:
                raise RecordNotFound(f"classification:{classification_id} not found")
            return self._classification_dict(row)

    def latest_classification_run(
        self, user_id: str, *, import_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._session() as session:
            statement = select(AIClassificationRunRow).where(
                AIClassificationRunRow.user_id == user_id
            )
            if import_id is not None:
                statement = statement.where(AIClassificationRunRow.import_id == import_id)
            row = session.scalar(
                statement.order_by(AIClassificationRunRow.created_at.desc()).limit(1)
            )
            return self._classification_dict(row) if row is not None else None

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

    @staticmethod
    def _id_field(kind: str) -> str:
        try:
            return CURRENT_KINDS[kind]
        except KeyError as exc:
            raise StorageError(f"unsupported current record kind {kind}") from exc

    @staticmethod
    def _analysis_dict(row: AnalysisRunRow) -> dict[str, Any]:
        result = row.result or {}
        if row.status == "QUEUED":
            default_analysis_status = "QUEUED"
        elif row.status == "FAILED":
            default_analysis_status = "FAILED"
        elif row.status == "COMPLETED":
            default_analysis_status = "SUCCEEDED"
        else:
            default_analysis_status = "RUNNING"
        default_interpretation_status = (
            "RUNNING"
            if row.status in {"INTERPRETATION_REQUESTING", "INTERPRETATION_VALIDATING"}
            else "NOT_REQUESTED"
        )
        return {
            "analysis_id": row.analysis_id,
            "user_id": row.user_id,
            "snapshot_id": row.snapshot_id,
            "status": row.status,
            "analysis_status": result.get("analysis_status", default_analysis_status),
            "interpretation_status": result.get(
                "interpretation_status", default_interpretation_status
            ),
            "execution_stage": result.get("execution_stage", row.status),
            "trigger_type": row.trigger_type,
            "metadata": row.metadata_json,
            "result": result or None,
            "error": row.error,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }

    @staticmethod
    def _interpretation_dict(row: AIInterpretationRunRow) -> dict[str, Any]:
        return {
            "interpretation_id": row.interpretation_id,
            "analysis_id": row.analysis_id,
            "snapshot_id": row.snapshot_id,
            "snapshot_revision": row.snapshot_revision,
            "request_id": row.request_id,
            "idempotency_key": row.idempotency_key,
            "contract_version": row.contract_version,
            "prompt_version": row.prompt_version,
            "model_name": row.model_name,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "fallback_used": row.fallback_used,
            "latency_ms": row.latency_ms,
            "response_payload": row.response_payload,
            "error_code": row.error_code,
            "created_at": row.created_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }

    @staticmethod
    def _classification_dict(row: AIClassificationRunRow) -> dict[str, Any]:
        return {
            "classification_id": row.classification_id,
            "user_id": row.user_id,
            "import_id": row.import_id,
            "request_id": row.request_id,
            "idempotency_key": row.idempotency_key,
            "label_set_hash": row.label_set_hash,
            "schema_version": row.schema_version,
            "contract_version": row.contract_version,
            "prompt_version": row.prompt_version,
            "mode": row.mode,
            "status": row.status,
            "applied": row.applied,
            "attempt_count": row.attempt_count,
            "fallback_used": row.fallback_used,
            "latency_ms": row.latency_ms,
            "model_name": row.model_name,
            "label_count": row.label_count,
            "group_count": row.group_count,
            "merged_label_count": row.merged_label_count,
            "response_payload": row.response_payload,
            "error_code": row.error_code,
            "created_at": row.created_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        }

    @staticmethod
    def _assert_classification_identity(
        row: AIClassificationRunRow,
        expected: Mapping[str, Any],
    ) -> None:
        mismatches = [field for field, value in expected.items() if getattr(row, field) != value]
        if mismatches:
            raise StorageConflict(
                "idempotency key was reused with different classification fields: "
                + ", ".join(mismatches)
            )

    @staticmethod
    def _complete_classification_success(
        session: Session,
        row: AIClassificationRunRow,
        completion: Mapping[str, Any],
    ) -> None:
        if completion.get("status") != "SUCCEEDED" or completion.get("applied") is not True:
            raise StorageError("atomic classification completion requires applied success")
        if row.mode != "ON":
            raise StorageError("only ON classification can be atomically applied")
        group_count = int(completion["group_count"])
        merged_label_count = int(completion["merged_label_count"])
        response_payload = completion.get("response_payload")
        FlowGuardRepository._validate_classification_success(
            row,
            group_count=group_count,
            merged_label_count=merged_label_count,
            response_payload=response_payload,
        )
        if row.status == "SUCCEEDED":
            FlowGuardRepository._assert_idempotent_classification_update(
                row,
                status="SUCCEEDED",
                applied=True,
                fallback_used=False,
                group_count=group_count,
                merged_label_count=merged_label_count,
                response_payload=response_payload,
                error_code=None,
            )
            return
        if row.status != "IN_PROGRESS":
            raise StorageConflict("classification terminal status is immutable")
        transitioned = session.execute(
            update(AIClassificationRunRow)
            .where(
                AIClassificationRunRow.classification_id == row.classification_id,
                AIClassificationRunRow.status == "IN_PROGRESS",
            )
            .values(
                status="SUCCEEDED",
                applied=True,
                attempt_count=int(completion["attempt_count"]),
                fallback_used=False,
                latency_ms=int(completion["latency_ms"]),
                group_count=group_count,
                merged_label_count=merged_label_count,
                response_payload=jsonable(response_payload),
                error_code=None,
                completed_at=utc_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if transitioned.rowcount != 1:
            raise StorageConflict("classification terminal transition lost a concurrent race")
        session.expire(row)

    @staticmethod
    def _verify_classification_persistence_guard(
        row: AIClassificationRunRow,
        guard: Mapping[str, Any],
    ) -> None:
        status = guard.get("status")
        if status not in {"SUCCEEDED", "REJECTED", "FAILED"}:
            raise StorageError("classification persistence guard requires terminal status")
        if guard.get("applied") is not False:
            raise StorageError("non-applied classification guard requires applied=false")
        if row.status != status or row.applied is not False:
            raise StorageConflict("classification persistence guard no longer matches")

    @staticmethod
    def _assert_idempotent_classification_update(
        row: AIClassificationRunRow,
        *,
        status: str,
        applied: bool | None,
        fallback_used: bool | None,
        group_count: int | None,
        merged_label_count: int | None,
        response_payload: Mapping[str, Any] | None,
        error_code: str | None,
    ) -> None:
        expected = {
            "status": status,
            **({"applied": applied} if applied is not None else {}),
            **({"fallback_used": fallback_used} if fallback_used is not None else {}),
            **({"group_count": group_count} if group_count is not None else {}),
            **(
                {"merged_label_count": merged_label_count} if merged_label_count is not None else {}
            ),
            **({"error_code": error_code} if error_code is not None else {}),
        }
        mismatches = [field for field, value in expected.items() if getattr(row, field) != value]
        if response_payload is not None and row.response_payload != jsonable(response_payload):
            mismatches.append("response_payload")
        if mismatches:
            raise StorageConflict(
                "classification terminal status is immutable: " + ", ".join(mismatches)
            )

    @staticmethod
    def _validate_classification_success(
        row: AIClassificationRunRow,
        *,
        group_count: int | None,
        merged_label_count: int | None,
        response_payload: Mapping[str, Any] | None,
    ) -> None:
        if group_count is None or merged_label_count is None or response_payload is None:
            raise StorageError(
                "successful classification requires payload and complete group counts"
            )
        if group_count + merged_label_count != row.label_count:
            raise StorageError("classification group counts do not cover the label set")
        if "labels" in response_payload:
            raise StorageError("classification audit payload must not contain request labels")

    @staticmethod
    def _assert_interpretation_identity(
        row: AIInterpretationRunRow,
        expected: Mapping[str, str],
    ) -> None:
        mismatches = [field for field, value in expected.items() if getattr(row, field) != value]
        if mismatches:
            raise StorageConflict(
                "idempotency key was reused with different interpretation fields: "
                + ", ".join(mismatches)
            )

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
