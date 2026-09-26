"""backtest_runs table for multiple-testing audit trail

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("strategy_name", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("params", postgresql.JSONB, nullable=False),
        sa.Column("agent_versions", postgresql.JSONB, nullable=False),
        sa.Column("symbols", postgresql.JSONB, nullable=False),
        sa.Column("split", sa.String(32), nullable=False),
        sa.Column("start_date", sa.String(32), nullable=False),
        sa.Column("end_date", sa.String(32), nullable=False),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("git_commit", sa.String(64)),
        sa.Column("metrics", postgresql.JSONB, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("status", sa.String(32), nullable=False, server_default="completed"),
    )
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])
    op.create_index("ix_backtest_runs_strategy_name", "backtest_runs", ["strategy_name"])


def downgrade() -> None:
    op.drop_index("ix_backtest_runs_strategy_name", table_name="backtest_runs")
    op.drop_index("ix_backtest_runs_created_at", table_name="backtest_runs")
    op.drop_table("backtest_runs")
