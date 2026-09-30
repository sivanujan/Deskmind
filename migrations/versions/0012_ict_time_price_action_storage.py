"""ICT Time and Price Action agent storage: compressed TimescaleDB hypertables.

- ict_time_items: symbol + agent_version + item_id + confirmed_at (no timeframe: symbol-level agent)
- price_action_items: symbol + timeframe + agent_version + item_id + confirmed_at

Both are hypertables on confirmed_at (30-day chunks), compressed after 30 days.
agent_item_transitions already covers both agents (shared table, agent column discriminates).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- ict_time_items
    op.create_table(
        "ict_time_items",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("agent_version", sa.String(16), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("pivot_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "symbol",
            "agent_version",
            "item_id",
            "confirmed_at",
            name="ict_time_items_pkey",
        ),
    )
    op.execute(
        "SELECT create_hypertable('ict_time_items', 'confirmed_at', "
        "chunk_time_interval => interval '30 days')"
    )
    op.execute(
        "ALTER TABLE ict_time_items SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'symbol, agent_version', "
        "timescaledb.compress_orderby = 'confirmed_at, item_id')"
    )
    op.execute("SELECT add_compression_policy('ict_time_items', interval '30 days')")

    # --- price_action_items
    op.create_table(
        "price_action_items",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("agent_version", sa.String(16), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("pivot_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "symbol",
            "timeframe",
            "agent_version",
            "item_id",
            "confirmed_at",
            name="price_action_items_pkey",
        ),
    )
    op.execute(
        "SELECT create_hypertable('price_action_items', 'confirmed_at', "
        "chunk_time_interval => interval '30 days')"
    )
    op.execute(
        "ALTER TABLE price_action_items SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'symbol, agent_version, timeframe', "
        "timescaledb.compress_orderby = 'confirmed_at, item_id')"
    )
    op.execute("SELECT add_compression_policy('price_action_items', interval '30 days')")


def downgrade() -> None:
    op.drop_table("price_action_items")
    op.drop_table("ict_time_items")
