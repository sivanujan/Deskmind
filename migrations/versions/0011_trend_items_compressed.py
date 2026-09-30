"""trend_items: trend agent items as a compressed TimescaleDB hypertable (trend 1.2.0).

Same design as ict_smc_items (migration 0010): latest payload per item, hypertable on
`confirmed_at` (constant per item since trend 1.1.1), compressed after 30 days. Trend
transitions go to agent_item_transitions (agent = 'trend'). Older trend versions' rows stay in
agent_events / agent_reports until the user confirms a cleanup.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trend_items",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("agent_version", sa.String(16), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("pivot_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "symbol",
            "timeframe",
            "agent_version",
            "item_id",
            "confirmed_at",
            name="trend_items_pkey",
        ),
    )
    op.execute(
        "SELECT create_hypertable('trend_items', 'confirmed_at', "
        "chunk_time_interval => interval '30 days')"
    )
    op.execute(
        "ALTER TABLE trend_items SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'symbol, agent_version, timeframe', "
        "timescaledb.compress_orderby = 'confirmed_at, item_id')"
    )
    op.execute("SELECT add_compression_policy('trend_items', interval '30 days')")


def downgrade() -> None:
    op.drop_table("trend_items")
