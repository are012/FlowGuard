"""Add AI label-classification audit runs.

Revision ID: 20260802_0002
Revises: 20260802_0001
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260802_0002"
down_revision: str | None = "20260802_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_classification_runs",
        sa.Column("classification_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("import_id", sa.String(length=128), nullable=False),
        sa.Column("request_id", sa.String(length=256), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("label_set_hash", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("contract_version", sa.String(length=32), nullable=False),
        sa.Column("prompt_version", sa.String(length=128), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("label_count", sa.Integer(), nullable=False),
        sa.Column("group_count", sa.Integer(), nullable=True),
        sa.Column("merged_label_count", sa.Integer(), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("classification_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_ai_classification_runs_import_id",
        "ai_classification_runs",
        ["import_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_classification_runs_request_id",
        "ai_classification_runs",
        ["request_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_classification_runs_user_id",
        "ai_classification_runs",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_classification_runs_user_id", table_name="ai_classification_runs")
    op.drop_index("ix_ai_classification_runs_request_id", table_name="ai_classification_runs")
    op.drop_index("ix_ai_classification_runs_import_id", table_name="ai_classification_runs")
    op.drop_table("ai_classification_runs")
