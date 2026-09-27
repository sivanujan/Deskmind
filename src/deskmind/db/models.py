"""ORM models. Prices and sizes are always Numeric, never float."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PRICE = Numeric(28, 10)
QTY = Numeric(28, 10)
EMBEDDING_DIM = 1024


class Base(DeclarativeBase):
    pass


class StrategyStatus(enum.StrEnum):
    untested = "untested"
    testing = "testing"
    validated = "validated"
    rejected = "rejected"


class Candle(Base):
    """OHLCV candle. TimescaleDB hypertable on open_time (see migration)."""

    __tablename__ = "candles"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    quote_volume: Mapped[Decimal] = mapped_column(QTY, nullable=False)


class AgentReport(Base):
    __tablename__ = "agent_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentEvent(Base):
    """An item produced by an agent (swing, break, trend line, ...), drawn as a chart overlay.

    PK (agent, symbol, timeframe, item_id); `payload` holds the full item including later updates
    (broken_at, touches, ...); `confirmed_at` = when it became knowable (replay filter).
    """

    __tablename__ = "agent_events"

    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    pivot_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TradeJournal(Base):
    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry: Mapped[Decimal | None] = mapped_column(PRICE)
    sl: Mapped[Decimal | None] = mapped_column(PRICE)
    tp: Mapped[Decimal | None] = mapped_column(PRICE)
    size: Mapped[Decimal | None] = mapped_column(QTY)
    leverage: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    pnl: Mapped[Decimal | None] = mapped_column(PRICE)
    head_agent_reasoning: Mapped[str | None] = mapped_column(Text)
    risk_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    exit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    fees_paid: Mapped[Decimal | None] = mapped_column(PRICE)
    trading_mode: Mapped[str] = mapped_column(String(16), server_default="paper", nullable=False)
    orders: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)


class StrategyLibrary(Base):
    __tablename__ = "strategy_library"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    data_source: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[StrategyStatus] = mapped_column(
        Enum(StrategyStatus, name="strategy_status"),
        nullable=False,
        server_default=StrategyStatus.untested.value,
    )
    backtest_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    source_trade_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("trade_journal.id", ondelete="SET NULL")
    )
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class BacktestRun(Base):
    """Immutable audit record of every backtest run.

    Multiple-testing accounting: failed and discarded runs are recorded alongside successes.
    """

    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    strategy_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    agent_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    split: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[str] = mapped_column(String(32), nullable=False)
    end_date: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    git_commit: Mapped[str | None] = mapped_column(String(64))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
