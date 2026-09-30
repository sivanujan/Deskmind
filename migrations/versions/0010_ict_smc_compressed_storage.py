"""ICT/SMC point-in-time storage as compressed TimescaleDB hypertables.

- agent_item_transitions -> hypertable on `at` (30-day chunks), columnstore compression segmented
  by (agent, symbol, agent_version, timeframe), ordered by (at, ord); chunks older than 7 days are
  compressed by policy. The primary key now includes `at` (required for hypertables).
- ict_smc_items (new): latest payload per item, hypertable on `confirmed_at` (an item's
  confirmed_at never changes), compressed after 30 days. Current-version (1.2.0) ICT items are
  moved here from agent_events; older versions stay in agent_events until the user confirms a
  cleanup.

Measured on BTCUSDT 88 days: transitions 22 MB -> 3.7 MB compressed.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "agent_item_transitions"


def upgrade() -> None:
    # --- transitions: time-including primary key, hypertable, compression
    op.drop_constraint("uq_agent_item_transitions_order", T, type_="unique")
    op.drop_constraint(f"{T}_pkey", T, type_="primary")
    op.drop_column(T, "id")
    op.create_primary_key(
        f"{T}_pkey", T, ["agent", "symbol", "agent_version", "timeframe", "at", "ord"]
    )
    op.execute(
        f"SELECT create_hypertable('{T}', 'at', chunk_time_interval => interval '30 days', "
        "migrate_data => true)"
    )
    op.execute(
        f"ALTER TABLE {T} SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'agent, symbol, agent_version, timeframe', "
        "timescaledb.compress_orderby = 'at, ord')"
    )
    op.execute(f"SELECT add_compression_policy('{T}', interval '7 days')")

    # --- items: dedicated compressed hypertable
    op.create_table(
        "ict_smc_items",
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
            name="ict_smc_items_pkey",
        ),
    )
    op.execute(
        "SELECT create_hypertable('ict_smc_items', 'confirmed_at', "
        "chunk_time_interval => interval '30 days')"
    )
    op.execute(
        "ALTER TABLE ict_smc_items SET (timescaledb.compress, "
        "timescaledb.compress_segmentby = 'symbol, agent_version, timeframe', "
        "timescaledb.compress_orderby = 'confirmed_at, item_id')"
    )
    op.execute("SELECT add_compression_policy('ict_smc_items', interval '30 days')")
    op.execute(
        "INSERT INTO ict_smc_items (symbol, timeframe, agent_version, item_id, kind, pivot_time, "
        "confirmed_at, payload, updated_at) "
        "SELECT symbol, timeframe, agent_version, item_id, kind, pivot_time, confirmed_at, "
        "payload, updated_at FROM agent_events WHERE agent = 'ict_smc' AND agent_version = '1.2.0'"
    )
    op.execute("DELETE FROM agent_events WHERE agent = 'ict_smc' AND agent_version = '1.2.0'")


def downgrade() -> None:
    op.execute(
        "INSERT INTO agent_events (agent, symbol, timeframe, item_id, kind, pivot_time, "
        "confirmed_at, agent_version, payload) SELECT 'ict_smc', symbol, timeframe, item_id, "
        "kind, pivot_time, confirmed_at, agent_version, payload FROM ict_smc_items "
        "ON CONFLICT DO NOTHING"
    )
    op.drop_table("ict_smc_items")
    op.execute(f"SELECT remove_compression_policy('{T}')")
    op.execute(
        f"CREATE TABLE {T}_plain AS SELECT * FROM {T}; DROP TABLE {T}; "
        f"ALTER TABLE {T}_plain RENAME TO {T}"
    )
    op.create_primary_key(
        f"{T}_pkey", T, ["agent", "symbol", "agent_version", "timeframe", "at", "ord"]
    )
