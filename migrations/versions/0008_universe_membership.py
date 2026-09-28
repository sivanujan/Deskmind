"""universe_membership (point-in-time daily membership) + universe_delistings.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "universe_membership",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("rank", sa.Integer, nullable=False),
        sa.Column("median_quote_volume", sa.Numeric(38, 4), nullable=False),
        sa.Column("tradeable", sa.Boolean, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),  # live | backfill
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_universe_membership_symbol_day", "universe_membership", ["symbol", "day"])
    op.create_table(
        "universe_delistings",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("delisted_at_ms", sa.BigInteger, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("universe_delistings")
    op.drop_table("universe_membership")
