"""Create the FlowGuard MVP persistence schema.

Revision ID: 20260802_0001
Revises:
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "current_records",
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("record_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "kind", "record_id"),
    )
    op.create_index(
        "ix_current_records_user_kind",
        "current_records",
        ["user_id", "kind"],
        unique=False,
    )
    op.create_table(
        "data_revisions",
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "financial_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )
    op.create_index(
        "ix_financial_snapshots_user_id", "financial_snapshots", ["user_id"], unique=False
    )
    op.create_table(
        "analysis_runs",
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("trigger_type", sa.String(length=64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["snapshot_id"], ["financial_snapshots.snapshot_id"]),
        sa.PrimaryKeyConstraint("analysis_id"),
    )
    op.create_index("ix_analysis_runs_snapshot_id", "analysis_runs", ["snapshot_id"], unique=False)
    op.create_index("ix_analysis_runs_status", "analysis_runs", ["status"], unique=False)
    op.create_index("ix_analysis_runs_user_id", "analysis_runs", ["user_id"], unique=False)
    op.create_table(
        "analysis_events",
        sa.Column("event_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_runs.analysis_id"]),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("analysis_id", "sequence", name="uq_analysis_event_sequence"),
    )
    op.create_index(
        "ix_analysis_events_analysis_id", "analysis_events", ["analysis_id"], unique=False
    )
    op.create_table(
        "analysis_reports",
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_runs.analysis_id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["financial_snapshots.snapshot_id"]),
        sa.PrimaryKeyConstraint("analysis_id"),
    )
    op.create_index(
        "ix_analysis_reports_created_at", "analysis_reports", ["created_at"], unique=False
    )
    op.create_index("ix_analysis_reports_user_id", "analysis_reports", ["user_id"], unique=False)
    op.create_table(
        "latest_report_pointers",
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_revision", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_reports.analysis_id"]),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("analysis_id"),
    )
    op.create_table(
        "ai_interpretation_runs",
        sa.Column("interpretation_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_revision", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=256), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_runs.analysis_id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["financial_snapshots.snapshot_id"]),
        sa.PrimaryKeyConstraint("interpretation_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_ai_interpretation_runs_analysis_id",
        "ai_interpretation_runs",
        ["analysis_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_interpretation_runs_request_id",
        "ai_interpretation_runs",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_interpretation_runs_snapshot_id",
        "ai_interpretation_runs",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_interpretation_runs_status", "ai_interpretation_runs", ["status"], unique=False
    )
    op.create_table(
        "recommendations",
        sa.Column("recommendation_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_runs.analysis_id"]),
        sa.PrimaryKeyConstraint("recommendation_id"),
    )
    op.create_index(
        "ix_recommendations_analysis_id", "recommendations", ["analysis_id"], unique=False
    )
    op.create_index("ix_recommendations_user_id", "recommendations", ["user_id"], unique=False)
    op.create_table(
        "approval_records",
        sa.Column("audit_id", sa.String(length=128), nullable=False),
        sa.Column("recommendation_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("effect", sa.String(length=40), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.recommendation_id"]),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "ix_approval_records_recommendation_id",
        "approval_records",
        ["recommendation_id"],
        unique=False,
    )
    op.create_index("ix_approval_records_user_id", "approval_records", ["user_id"], unique=False)
    op.create_table(
        "tool_executions",
        sa.Column("tool_execution_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("agent_run_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["analysis_runs.analysis_id"]),
        sa.PrimaryKeyConstraint("tool_execution_id"),
        sa.UniqueConstraint(
            "analysis_id", "agent_run_id", "sequence", name="uq_tool_execution_sequence"
        ),
    )
    op.create_index(
        "ix_tool_executions_agent_run_id", "tool_executions", ["agent_run_id"], unique=False
    )
    op.create_index(
        "ix_tool_executions_analysis_id", "tool_executions", ["analysis_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_tool_executions_analysis_id", table_name="tool_executions")
    op.drop_index("ix_tool_executions_agent_run_id", table_name="tool_executions")
    op.drop_table("tool_executions")
    op.drop_index("ix_approval_records_user_id", table_name="approval_records")
    op.drop_index("ix_approval_records_recommendation_id", table_name="approval_records")
    op.drop_table("approval_records")
    op.drop_index("ix_recommendations_user_id", table_name="recommendations")
    op.drop_index("ix_recommendations_analysis_id", table_name="recommendations")
    op.drop_table("recommendations")
    op.drop_index("ix_ai_interpretation_runs_status", table_name="ai_interpretation_runs")
    op.drop_index("ix_ai_interpretation_runs_snapshot_id", table_name="ai_interpretation_runs")
    op.drop_index("ix_ai_interpretation_runs_request_id", table_name="ai_interpretation_runs")
    op.drop_index("ix_ai_interpretation_runs_analysis_id", table_name="ai_interpretation_runs")
    op.drop_table("ai_interpretation_runs")
    op.drop_table("latest_report_pointers")
    op.drop_index("ix_analysis_reports_user_id", table_name="analysis_reports")
    op.drop_index("ix_analysis_reports_created_at", table_name="analysis_reports")
    op.drop_table("analysis_reports")
    op.drop_index("ix_analysis_events_analysis_id", table_name="analysis_events")
    op.drop_table("analysis_events")
    op.drop_index("ix_analysis_runs_user_id", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_status", table_name="analysis_runs")
    op.drop_index("ix_analysis_runs_snapshot_id", table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_index("ix_financial_snapshots_user_id", table_name="financial_snapshots")
    op.drop_table("financial_snapshots")
    op.drop_table("data_revisions")
    op.drop_index("ix_current_records_user_kind", table_name="current_records")
    op.drop_table("current_records")
