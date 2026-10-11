"""universe_symbols + universe_runs: trading universe lifecycle and daily run summaries.

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

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "universe_symbols",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("in_universe", sa.Boolean, nullable=False),
        sa.Column("rank", sa.Integer),
        sa.Column("quote_volume_24h", sa.Numeric(38, 8)),
        sa.Column("onboard_at", TS),
        sa.Column("entered_at", TS),
        sa.Column("exited_at", TS),
        sa.Column("state_changed_at", TS),
        sa.Column("verified_at", TS),
        sa.Column("warmed_up_at", TS),
        sa.Column("blocked_reason", sa.String(32)),
        sa.Column("last_error", sa.Text),
        sa.Column("history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_universe_symbols_state", "universe_symbols", ["state"])
    op.create_table(
        "universe_runs",
        sa.Column("run_date", sa.Date, primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("summary", JSONB),
    )


def downgrade() -> None:
    op.drop_table("universe_runs")
    op.drop_index("ix_universe_symbols_state", table_name="universe_symbols")
    op.drop_table("universe_symbols")
