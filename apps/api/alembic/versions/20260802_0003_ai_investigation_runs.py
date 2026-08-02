"""Add AI investigation run and turn audit tables.

Revision ID: 20260802_0003
Revises: 20260802_0002
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0003"
down_revision: str | None = "20260802_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_investigation_runs",
        sa.Column("investigation_id", sa.String(length=128), nullable=False),
        sa.Column("analysis_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_revision", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=256), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("tool_call_count", sa.Integer(), nullable=False),
        sa.Column("total_latency_ms", sa.Integer(), nullable=False),
        sa.Column("additional_investigation_requested", sa.Boolean(), nullable=False),
        sa.Column("hypotheses", sa.JSON(), nullable=False),
        sa.Column("priorities", sa.JSON(), nullable=False),
        sa.Column("unresolved", sa.JSON(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_id"],
            ["analysis_runs.analysis_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["financial_snapshots.snapshot_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("investigation_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_ai_investigation_runs_analysis_id",
        "ai_investigation_runs",
        ["analysis_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_investigation_runs_request_id",
        "ai_investigation_runs",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_investigation_runs_snapshot_id",
        "ai_investigation_runs",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_investigation_runs_status",
        "ai_investigation_runs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_ai_investigation_runs_user_id",
        "ai_investigation_runs",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "ai_investigation_turns",
        sa.Column("turn_id", sa.String(length=128), nullable=False),
        sa.Column("investigation_id", sa.String(length=128), nullable=False),
        sa.Column("turn_sequence", sa.Integer(), nullable=False),
        sa.Column("phase", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["investigation_id"],
            ["ai_investigation_runs.investigation_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("turn_id"),
        sa.UniqueConstraint(
            "investigation_id",
            "turn_sequence",
            name="uq_ai_investigation_turn_sequence",
        ),
    )
    op.create_index(
        "ix_ai_investigation_turns_investigation_id",
        "ai_investigation_turns",
        ["investigation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_investigation_turns_investigation_id",
        table_name="ai_investigation_turns",
    )
    op.drop_table("ai_investigation_turns")
    op.drop_index("ix_ai_investigation_runs_user_id", table_name="ai_investigation_runs")
    op.drop_index("ix_ai_investigation_runs_status", table_name="ai_investigation_runs")
    op.drop_index("ix_ai_investigation_runs_snapshot_id", table_name="ai_investigation_runs")
    op.drop_index("ix_ai_investigation_runs_request_id", table_name="ai_investigation_runs")
    op.drop_index("ix_ai_investigation_runs_analysis_id", table_name="ai_investigation_runs")
    op.drop_table("ai_investigation_runs")
