"""agent_events table for agent items (overlays/replay); as_of + unique key on agent_reports

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("pivot_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_version", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("agent", "symbol", "timeframe", "item_id", name="pk_agent_events"),
    )
    op.create_index(
        "ix_agent_events_lookup",
        "agent_events",
        ["agent", "symbol", "timeframe", "confirmed_at"],
    )
    op.create_index(
        "ix_agent_events_pivot", "agent_events", ["agent", "symbol", "timeframe", "pivot_time"]
    )

    op.add_column("agent_reports", sa.Column("as_of", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_agent_reports_as_of",
        "agent_reports",
        ["agent_name", "symbol", "timeframe", "version", "as_of"],
        unique=True,
        postgresql_where=sa.text("as_of IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_reports_as_of", table_name="agent_reports")
    op.drop_column("agent_reports", "as_of")
    op.drop_index("ix_agent_events_pivot", table_name="agent_events")
    op.drop_index("ix_agent_events_lookup", table_name="agent_events")
    op.drop_table("agent_events")
