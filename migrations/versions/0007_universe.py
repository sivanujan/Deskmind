"""universe_symbols + universe_runs: liquidity universe lifecycle and daily job summaries.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "universe_symbols",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("rank", sa.Integer),
        sa.Column("median_quote_volume", sa.Numeric(38, 4)),
        sa.Column("onboard_ms", sa.BigInteger),
        sa.Column("member", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("data_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("warmed_up", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text),
        sa.Column("entered_at_ms", sa.BigInteger),
        sa.Column("exited_at_ms", sa.BigInteger),
        sa.Column("state_changed_at_ms", sa.BigInteger),
        sa.Column("history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_universe_symbols_state", "universe_symbols", ["state"])
    op.create_table(
        "universe_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("summary", JSONB, nullable=False),
    )
    op.create_index("ix_universe_runs_started_at", "universe_runs", ["started_at"])


def downgrade() -> None:
    op.drop_table("universe_runs")
    op.drop_table("universe_symbols")
