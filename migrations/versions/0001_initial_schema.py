"""initial schema: extensions, candles hypertable, journals, strategies, lessons

Revision ID: 0001
Revises:
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(28, 10)
QTY = sa.Numeric(28, 10)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", QTY, nullable=False),
        sa.Column("quote_volume", QTY, nullable=False),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "open_time", name="pk_candles"),
    )
    op.execute(
        "SELECT create_hypertable('candles', 'open_time', "
        "chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE)"
    )

    op.create_table(
        "agent_reports",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("agent_name", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
    )
    op.create_index("ix_agent_reports_created_at", "agent_reports", ["created_at"])
    op.create_index("ix_agent_reports_agent_name", "agent_reports", ["agent_name"])

    op.create_table(
        "trade_journal",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("entry", PRICE),
        sa.Column("sl", PRICE),
        sa.Column("tp", PRICE),
        sa.Column("size", QTY),
        sa.Column("leverage", sa.Integer),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("pnl", PRICE),
        sa.Column("head_agent_reasoning", sa.Text),
        sa.Column("risk_decision", postgresql.JSONB),
        sa.Column("client_order_id", sa.String(64), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_trade_journal_client_order_id"),
    )
    op.create_index("ix_trade_journal_created_at", "trade_journal", ["created_at"])

    strategy_status = postgresql.ENUM(
        "untested", "testing", "validated", "rejected", name="strategy_status"
    )
    op.create_table(
        "strategy_library",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("data_source", sa.String(128)),
        sa.Column("status", strategy_status, nullable=False, server_default="untested"),
        sa.Column("backtest_summary", postgresql.JSONB),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("name", name="uq_strategy_library_name"),
    )

    op.create_table(
        "lessons",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column(
            "source_trade_id",
            sa.BigInteger,
            sa.ForeignKey("trade_journal.id", ondelete="SET NULL"),
        ),
        sa.Column("approved", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_table("lessons")
    op.drop_table("strategy_library")
    op.execute("DROP TYPE IF EXISTS strategy_status")
    op.drop_index("ix_trade_journal_created_at", table_name="trade_journal")
    op.drop_table("trade_journal")
    op.drop_index("ix_agent_reports_agent_name", table_name="agent_reports")
    op.drop_index("ix_agent_reports_created_at", table_name="agent_reports")
    op.drop_table("agent_reports")
    op.drop_table("candles")
