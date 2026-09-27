"""external_trades: the user's own Binance trade history (read-only import), kept apart from
trade_journal (Deskmind's own trades).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NUM = sa.Numeric(38, 18)


def upgrade() -> None:
    op.create_table(
        "external_trades",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("source", sa.String(8), nullable=False),  # api | csv
        sa.Column("market", sa.String(8), nullable=False),  # spot | futures
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),  # BUY | SELL
        sa.Column("price", NUM, nullable=False),
        sa.Column("qty", NUM, nullable=False),
        sa.Column("quote_qty", NUM),
        sa.Column("fee", NUM),
        sa.Column("fee_asset", sa.String(16)),
        sa.Column("realized_pnl", NUM),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("order_id", sa.String(64)),
        sa.Column("trade_id", sa.String(96), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "source", "market", "symbol", "trade_id", name="uq_external_trades_source_trade"
        ),
    )
    op.create_index("ix_external_trades_symbol_time", "external_trades", ["symbol", "time"])
    op.create_index("ix_external_trades_market_time", "external_trades", ["market", "time"])


def downgrade() -> None:
    op.drop_index("ix_external_trades_market_time", table_name="external_trades")
    op.drop_index("ix_external_trades_symbol_time", table_name="external_trades")
    op.drop_table("external_trades")
