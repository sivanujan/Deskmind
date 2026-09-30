"""agent_item_transitions: item + state transitions (point-in-time source of truth).

Replaces storing a full multi-timeframe report per bar for backfills: state as of any T is
rebuilt from these rows (see deskmind.agents.ict_smc.history).

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_item_transitions",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("agent", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("agent_version", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ord", sa.Integer, nullable=False),
        sa.Column("item_id", sa.String(96), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.UniqueConstraint(
            "agent",
            "symbol",
            "agent_version",
            "timeframe",
            "at",
            "ord",
            name="uq_agent_item_transitions_order",
        ),
    )


def downgrade() -> None:
    op.drop_table("agent_item_transitions")
