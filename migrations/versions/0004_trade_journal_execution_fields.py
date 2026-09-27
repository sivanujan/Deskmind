"""Add execution lifecycle fields to trade_journal

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

PRICE = sa.Numeric(18, 8)

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trade_journal", sa.Column("exit_price", PRICE, nullable=True))
    op.add_column(
        "trade_journal", sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("trade_journal", sa.Column("exit_reason", sa.String(32), nullable=True))
    op.add_column("trade_journal", sa.Column("fees_paid", PRICE, nullable=True))
    op.add_column(
        "trade_journal",
        sa.Column("trading_mode", sa.String(16), nullable=False, server_default="paper"),
    )
    op.add_column("trade_journal", sa.Column("orders", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("trade_journal", "orders")
    op.drop_column("trade_journal", "trading_mode")
    op.drop_column("trade_journal", "fees_paid")
    op.drop_column("trade_journal", "exit_reason")
    op.drop_column("trade_journal", "exit_time")
    op.drop_column("trade_journal", "exit_price")
