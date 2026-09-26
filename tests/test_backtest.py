"""Comprehensive unit and scenario tests for the Backtest Engine."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from deskmind.backtest.baselines import (
    calculate_single_buy_and_hold,
)
from deskmind.backtest.engine import (
    BacktestEngine,
)
from deskmind.backtest.filters import (
    SymbolFilters,
    round_price,
    round_qty,
    validate_order,
)
from deskmind.backtest.liquidation import (
    compute_liquidation_price,
    is_liquidated,
)
from deskmind.backtest.registry import validate_split_access
from deskmind.backtest.sizing import FixedFractionalSizer
from deskmind.data.storage import (
    FUNDING_SCHEMA,
    KLINE_SCHEMA,
    MARK_KLINE_SCHEMA,
)
from deskmind.strategies.base import (
    BaseStrategy,
    Intent,
    StrategyContext,
)


def _setup_symbol_data(
    data_dir: Path,
    symbol: str,
    klines_5m: list[dict[str, Any]],
    mark_5m: list[dict[str, Any]] | None = None,
    funding: list[tuple[int, float]] | None = None,
    tick_size: str = "0.1",
    step_size: str = "0.001",
    min_qty: str = "0.001",
    min_notional: str = "5.0",
) -> None:
    """Helper to populate fake parquet klines and meta filters."""
    # 1. Meta filters
    meta_dir = data_dir / "meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_file = meta_dir / f"{symbol}_filters_2024-01-01.json"
    meta_data = {
        "symbol": symbol,
        "summary": {
            "tickSize": tick_size,
            "stepSize": step_size,
            "minQty": min_qty,
            "maxQty": "1000.0",
            "minNotional": min_notional,
            "pricePrecision": 2,
            "quantityPrecision": 3,
        },
        "filters": {
            "PRICE_FILTER": {"tickSize": tick_size},
            "LOT_SIZE": {"stepSize": step_size, "minQty": min_qty},
            "MIN_NOTIONAL": {"minNotional": min_notional},
        },
    }
    meta_file.write_text(json.dumps(meta_data), encoding="utf-8")

    # 2. 5m klines
    p_dir = data_dir / "parquet" / symbol / "klines" / "5m" / "year=2024" / "month=01"
    p_dir.mkdir(parents=True, exist_ok=True)

    def to_pa_kline(rows):
        return pa.Table.from_pydict(
            {
                "open_time": [datetime.fromtimestamp(r["open_time"] / 1000, tz=UTC) for r in rows],
                "open": [float(r["open"]) for r in rows],
                "high": [float(r["high"]) for r in rows],
                "low": [float(r["low"]) for r in rows],
                "close": [float(r["close"]) for r in rows],
                "volume": [float(r.get("volume", 10.0)) for r in rows],
                "close_time": [
                    datetime.fromtimestamp(r["close_time"] / 1000, tz=UTC) for r in rows
                ],
                "quote_volume": [float(r.get("volume", 10.0) * r["close"]) for r in rows],
                "trades": [int(r.get("trades", 10)) for r in rows],
                "taker_buy_volume": [float(r.get("volume", 10.0) / 2) for r in rows],
                "taker_buy_quote_volume": [
                    float(r.get("volume", 10.0) * r["close"] / 2) for r in rows
                ],
            },
            schema=KLINE_SCHEMA,
        )

    pq.write_table(to_pa_kline(klines_5m), p_dir / "part.parquet")

    # Empty 15m, 1h, 2h to avoid loader errors
    for tf in ("15m", "1h", "2h"):
        tf_dir = data_dir / "parquet" / symbol / "klines" / tf / "year=2024" / "month=01"
        tf_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(to_pa_kline([]), tf_dir / "part.parquet")

    # 3. Mark klines
    m_dir = data_dir / "parquet" / symbol / "mark_klines" / "5m" / "year=2024" / "month=01"
    m_dir.mkdir(parents=True, exist_ok=True)
    m_rows = mark_5m or [
        {
            "open_time": r["open_time"],
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"],
            "close_time": r["close_time"],
        }
        for r in klines_5m
    ]
    m_table = pa.Table.from_pydict(
        {
            "open_time": [datetime.fromtimestamp(r["open_time"] / 1000, tz=UTC) for r in m_rows],
            "open": [float(r["open"]) for r in m_rows],
            "high": [float(r["high"]) for r in m_rows],
            "low": [float(r["low"]) for r in m_rows],
            "close": [float(r["close"]) for r in m_rows],
            "close_time": [datetime.fromtimestamp(r["close_time"] / 1000, tz=UTC) for r in m_rows],
        },
        schema=MARK_KLINE_SCHEMA,
    )
    pq.write_table(m_table, m_dir / "part.parquet")

    # 4. Funding
    f_dir = data_dir / "parquet" / symbol / "funding" / "year=2024"
    f_dir.mkdir(parents=True, exist_ok=True)
    f_items = funding or []
    f_table = pa.Table.from_pydict(
        {
            "funding_time": [datetime.fromtimestamp(t / 1000, tz=UTC) for t, _ in f_items],
            "funding_rate": [rate for _, rate in f_items],
            "mark_price": [100.0 for _ in f_items],
            "funding_interval_hours": [8 for _ in f_items],
        },
        schema=FUNDING_SCHEMA,
    )
    pq.write_table(f_table, f_dir / "part.parquet")


class ScriptedStrategy(BaseStrategy):
    """Strategy emitting predetermined intents on specific bar timestamps."""

    name = "scripted"
    version = "1.0.0"

    def __init__(self, intents_by_time: dict[int, list[Intent]]):
        super().__init__()
        self.intents_by_time = intents_by_time

    def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
        for ctx in contexts.values():
            t = ctx.as_of_ms
            if t in self.intents_by_time:
                return self.intents_by_time[t]
        return []


# ==============================================================================
# 1. Hand-computed micro scenarios
# ==============================================================================


def test_micro_scenario_long_win(tmp_path: Path):
    """Verify market entry next bar open, take profit fill, gross and net PnL and R-multiple."""
    # Bar 0: 1704067200000 (2024-01-01 00:00:00) -> close at 00:05 -> strategy emits Intent
    # Bar 1: 1704067500000 (2024-01-01 00:05:00) -> opens at 100.0 -> fills market order
    # Bar 2: 1704067800000 (2024-01-01 00:10:00) -> high touches 106.0 -> TP at 105.0 fills
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 102.0,
            "low": 99.5,
            "close": 101.0,
        },
        {
            "open_time": t2,
            "close_time": t3,
            "open": 101.0,
            "high": 106.0,
            "low": 100.5,
            "close": 105.5,
        },
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("105.0"),
    )
    strategy = ScriptedStrategy({t1: [intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),  # 1% of 10000 = 100 risk
    )
    res = engine.run()

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr.side == "long"
    assert tr.entry_price == Decimal("100.0")
    assert tr.exit_price == Decimal("105.0")
    assert tr.exit_reason == "take_profit"
    # Risk distance: 100 - 95 = 5. Qty = 100 / 5 = 20.0
    assert tr.qty == Decimal("20.0")
    # Gross PnL: (105 - 100) * 20 = 100.0. Fees: 1.42. Net PnL: 98.58
    assert tr.fees_paid == Decimal("1.42")
    assert tr.pnl == Decimal("98.58")
    assert tr.r_multiple == pytest.approx(Decimal("0.9858"), abs=Decimal("0.001"))


def test_micro_scenario_long_loss(tmp_path: Path):
    """Verify market entry next bar open, stop loss fill, negative PnL and R-multiple."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 99.5,
        },
        {
            "open_time": t2,
            "close_time": t3,
            "open": 99.5,
            "high": 100.0,
            "low": 94.0,
            "close": 94.5,
        },
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("110.0"),
    )
    strategy = ScriptedStrategy({t1: [intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr.side == "long"
    assert tr.entry_price == Decimal("100.0")
    assert tr.exit_price == Decimal("95.0")
    assert tr.exit_reason == "stop_loss"
    assert tr.fees_paid == Decimal("1.95")
    assert tr.pnl == Decimal("-101.95")
    assert tr.r_multiple == pytest.approx(Decimal("-1.0195"), abs=Decimal("0.001"))


def test_micro_scenario_short_win(tmp_path: Path):
    """Verify short trade hitting Take Profit at 2R."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 100.5,
            "low": 99.0,
            "close": 99.5,
        },
        {
            "open_time": t2,
            "close_time": t3,
            "open": 99.5,
            "high": 100.0,
            "low": 89.0,
            "close": 89.5,
        },
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_short",
        order_type="market",
        stop_loss=Decimal("105.0"),
        take_profit=Decimal("90.0"),
    )
    strategy = ScriptedStrategy({t1: [intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()

    assert len(res.trades) == 1
    tr = res.trades[0]
    assert tr.side == "short"
    assert tr.entry_price == Decimal("100.0")
    assert tr.exit_price == Decimal("90.0")
    assert tr.exit_reason == "take_profit"
    # Short PnL: (100 - 90) * 20 = 200.0. Fees: 1.00 taker + 0.36 maker = 1.36. Net PnL: 198.64
    assert tr.fees_paid == Decimal("1.36")
    assert tr.pnl == Decimal("198.64")
    assert tr.r_multiple == pytest.approx(Decimal("1.9864"), abs=Decimal("0.001"))


def test_fee_and_funding_math(tmp_path: Path):
    """Verify funding payments are correctly deducted for longs at funding timestamps."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t2,
            "close_time": t3,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
    ]
    # Funding timestamp occurs at t1
    funding = [(t1, 0.01)]  # 1% funding
    _setup_symbol_data(tmp_path, "BTCUSDT", klines, funding=funding)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("90.0"),
        take_profit=Decimal("120.0"),
    )
    # Manual close at t2 (Bar 1 close, so it executes on Bar 2 open)
    close_intent = Intent(symbol="BTCUSDT", action="close")
    strategy = ScriptedStrategy({t1: [intent], t2: [close_intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0"),
        taker_fee_rate=Decimal("0.0"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()
    assert len(res.trades) == 1
    tr = res.trades[0]
    # Position: risk 100 / 10 = 10 BTC. Notional = 10 * 100 = 1000.
    # Long pays positive funding: 1000 * 0.01 = 10.0
    assert tr.funding_paid == Decimal("10.0")


def test_sl_gap_through_fill(tmp_path: Path):
    """If next bar opens beyond SL, fill must happen at open, NOT at unreached stop."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 98.0,
            "close": 99.0,
        },
        # Bar 2 gaps down to 90.0, below SL 95.0
        {"open_time": t2, "close_time": t3, "open": 90.0, "high": 92.0, "low": 88.0, "close": 89.0},
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("110.0"),
    )
    strategy = ScriptedStrategy({t1: [intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0"),
        taker_fee_rate=Decimal("0.0"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()
    assert len(res.trades) == 1
    tr = res.trades[0]
    # Gapped through SL 95.0 -> fills at bar open 90.0
    assert tr.exit_price == Decimal("90.0")
    assert tr.exit_reason == "stop_loss"


def test_sl_tp_same_bar_conservative(tmp_path: Path):
    """When both SL and TP are hit in the same bar, assume SL hit first (conservative)."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        # Bar 2 has massive range: high 115 (above TP 110), low 85 (below SL 90)
        {
            "open_time": t2,
            "close_time": t3,
            "open": 100.0,
            "high": 115.0,
            "low": 85.0,
            "close": 102.0,
        },
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("90.0"),
        take_profit=Decimal("110.0"),
    )
    strategy = ScriptedStrategy({t1: [intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0"),
        taker_fee_rate=Decimal("0.0"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()
    assert len(res.trades) == 1
    tr = res.trades[0]
    # SL assumed hit first
    assert tr.exit_reason == "sl_tp_same_bar"
    assert tr.exit_price == Decimal("90.0")
    assert res.same_bar_sl_tp_count == 1


def test_limit_touch_vs_trade_through(tmp_path: Path):
    """Limit order must NOT fill if price only touches the limit; it MUST trade through."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000
    t4 = t3 + 300_000

    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        # Bar 1: low touches 98.0 exactly, does not trade through
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 98.0,
            "close": 99.0,
        },
        # Bar 2: low trades through to 97.5 (< 98.0)
        {
            "open_time": t2,
            "close_time": t3,
            "open": 99.0,
            "high": 100.0,
            "low": 97.5,
            "close": 98.5,
        },
        # Bar 3: close out
        {
            "open_time": t3,
            "close_time": t4,
            "open": 98.5,
            "high": 102.0,
            "low": 98.0,
            "close": 101.0,
        },
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="limit",
        limit_price=Decimal("98.0"),
        stop_loss=Decimal("90.0"),
        take_profit=Decimal("110.0"),
    )
    strategy = ScriptedStrategy({t1: [intent], t3: [Intent(symbol="BTCUSDT", action="close")]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0"),
        taker_fee_rate=Decimal("0.0"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()
    # At Bar 1, price only touched 98.0 -> not filled.
    # At Bar 2, price went to 97.5 -> filled!
    assert len(res.trades) == 1
    assert res.trades[0].entry_time_ms == t2
    assert res.trades[0].entry_price == Decimal("98.0")


def test_filter_rounding_and_rejection():
    """Verify tickSize, stepSize rounding and minNotional rejection."""
    filters = SymbolFilters(
        symbol="BTCUSDT",
        tick_size=Decimal("0.5"),
        step_size=Decimal("0.01"),
        min_qty=Decimal("0.05"),
        max_qty=Decimal("1000.0"),
        min_notional=Decimal("100.0"),
        price_precision=2,
        quantity_precision=3,
    )
    # Rounding
    p = round_price(Decimal("100.24"), filters.tick_size)
    assert p == Decimal("100.0")
    p2 = round_price(Decimal("100.35"), filters.tick_size)
    assert p2 == Decimal("100.5")

    q = round_qty(Decimal("1.2345"), filters.step_size)
    assert q == Decimal("1.23")

    # Rejection under min_qty
    valid, reason = validate_order(Decimal("100.0"), Decimal("0.04"), filters)
    assert not valid
    assert "minQty" in reason

    # Rejection under min_notional (0.06 * 1000 = 60 < 100)
    valid2, reason2 = validate_order(Decimal("1000.0"), Decimal("0.06"), filters)
    assert not valid2
    assert "minNotional" in reason2

    # Acceptance
    valid3, reason3 = validate_order(Decimal("2000.0"), Decimal("0.1"), filters)
    assert valid3
    assert reason3 == "OK"


# ==============================================================================
# 2. Liquidation scenario with mark price divergence
# ==============================================================================


def test_liquidation_scenario_mark_price_divergence(tmp_path: Path):
    """Test liquidation triggered by Mark price crossing threshold even if last price does not."""
    t0 = 1704067200000
    t1 = t0 + 300_000
    t2 = t1 + 300_000
    t3 = t2 + 300_000

    # Last price never drops below 92.0
    klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t2,
            "close_time": t3,
            "open": 99.0,
            "high": 100.0,
            "low": 92.0,
            "close": 93.0,
        },
    ]
    # Mark price plunges to 80.0 at Bar 2
    mark_klines = [
        {
            "open_time": t0,
            "close_time": t1,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t1,
            "close_time": t2,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
        },
        {
            "open_time": t2,
            "close_time": t3,
            "open": 99.0,
            "high": 100.0,
            "low": 80.0,
            "close": 82.0,
        },
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines, mark_5m=mark_klines)

    # 10x leverage long entered at 100.0
    # Liquidation price with 1% MMR and 10x leverage:
    # 100 * (1 - 0.10 + 0.01) / (1 - 0.01) = 91 / 0.99 = ~91.919
    liq_p = compute_liquidation_price(
        side="long",
        entry_price=Decimal("100.0"),
        leverage=10,
        qty=Decimal("10.0"),
    )
    assert Decimal("90.0") < liq_p < Decimal("92.0")

    # If evaluated against last price (low=92.0), it would NOT liquidate (or barely touch).
    # But against mark price (low=80.0), it definitely liquidates!
    assert is_liquidated("long", liq_p, mark_high=Decimal("100.0"), mark_low=Decimal("80.0"))

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("50.0"),
        take_profit=Decimal("200.0"),
    )
    strategy = ScriptedStrategy({t1: [intent]})

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0"),
        taker_fee_rate=Decimal("0.0"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.05"), max_leverage=10),
        leverage=10,
    )
    res = engine.run()
    assert len(res.trades) == 1
    assert res.trades[0].exit_reason == "liquidation"
    assert res.liquidations_count == 1


# ==============================================================================
# 3. Buy & Hold baseline simulation
# ==============================================================================


def test_buy_and_hold_baseline_simulation():
    """Verify Buy & Hold return equals price change minus taker fees and funding."""
    start_price = Decimal("100.0")
    end_price = Decimal("120.0")
    alloc_cash = Decimal("10000.0")
    funding_rates = [Decimal("0.005")]
    taker_fee_rate = Decimal("0.0005")

    bnh = calculate_single_buy_and_hold(
        symbol="BTCUSDT",
        start_price=start_price,
        end_price=end_price,
        alloc_cash=alloc_cash,
        funding_rates=funding_rates,
        taker_fee_rate=taker_fee_rate,
    )
    # Price moved from 100 to 120 (+20%)
    # Qty bought at 100: 10000 / 100 = 100 BTC
    # Entry fee: 10000 * 0.0005 = 5.0
    # Funding paid: 10000 * 0.005 = 50.0
    # Exit fee at 120: (100 * 120) * 0.0005 = 12000 * 0.0005 = 6.0
    # Total fees: 5.0 + 6.0 = 11.0
    # Gross PnL: (120 - 100) * 100 = 2000.0
    # Net PnL: 2000.0 - 11.0 - 50.0 = 1939.0
    # Net return pct: (1939.0 / 10000.0) * 100 = 19.39%
    assert bnh.price_return_pct == pytest.approx(20.0)
    assert bnh.fees_paid_usd == pytest.approx(11.0)
    assert bnh.funding_paid_usd == pytest.approx(50.0)
    assert bnh.net_return_pct == pytest.approx(19.39)


# ==============================================================================
# 4. No look-ahead test
# ==============================================================================


def test_no_look_ahead_consistency(tmp_path: Path):
    """Decisions up to time T must be identical whether data ends at T or continues past T."""
    t0 = 1704067200000
    klines_full = []
    for i in range(12):
        t = t0 + i * 300_000
        klines_full.append(
            {
                "open_time": t,
                "close_time": t + 300_000,
                "open": 100.0 + i,
                "high": 102.0 + i,
                "low": 99.0 + i,
                "close": 101.0 + i,
            }
        )

    dir_full = tmp_path / "full"
    dir_half = tmp_path / "half"
    _setup_symbol_data(dir_full, "BTCUSDT", klines_full)
    _setup_symbol_data(dir_half, "BTCUSDT", klines_full[:6])

    recorded_half = []
    recorded_full = []

    class ObserverStrategy(BaseStrategy):
        name = "observer"
        version = "1.0.0"

        def __init__(self, target_list):
            super().__init__()
            self.target_list = target_list

        def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
            for ctx in contexts.values():
                self.target_list.append((ctx.as_of_ms, float(ctx.current_bar.close)))
            return []

    # Run half
    eng_half = BacktestEngine(
        strategy=ObserverStrategy(recorded_half),
        symbols=["BTCUSDT"],
        data_dir=dir_half,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
    )
    eng_half.run()

    # Run full
    eng_full = BacktestEngine(
        strategy=ObserverStrategy(recorded_full),
        symbols=["BTCUSDT"],
        data_dir=dir_full,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
    )
    eng_full.run()

    # The bars seen in half run must be identical prefix of full run
    assert len(recorded_half) == 6
    assert recorded_half == recorded_full[:6]


# ==============================================================================
# 5. Determinism test
# ==============================================================================


def test_engine_determinism(tmp_path: Path):
    """Identical inputs and seed must produce bit-for-bit identical results."""
    t0 = 1704067200000
    klines = [
        {
            "open_time": t0 + i * 300_000,
            "close_time": t0 + (i + 1) * 300_000,
            "open": 100.0 + i,
            "high": 102.0 + i,
            "low": 99.0 + i,
            "close": 101.0 + i,
        }
        for i in range(10)
    ]
    _setup_symbol_data(tmp_path, "BTCUSDT", klines)

    intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("95.0"),
        take_profit=Decimal("110.0"),
    )

    def run_simulation():
        strategy = ScriptedStrategy({t0 + 300_000: [intent]})
        eng = BacktestEngine(
            strategy=strategy,
            symbols=["BTCUSDT"],
            data_dir=tmp_path,
            start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
            initial_cash=Decimal("10000.0"),
        )
        return eng.run()

    r1 = run_simulation()
    r2 = run_simulation()

    assert r1.final_equity == r2.final_equity
    assert len(r1.trades) == len(r2.trades)
    assert r1.trades[0].pnl == r2.trades[0].pnl
    assert r1.trades[0].fees_paid == r2.trades[0].fees_paid


# ==============================================================================
# 6. Test split lock refusal
# ==============================================================================


def test_test_split_refuses_without_unlock_flag():
    """Running on the TEST split must raise PermissionError unless explicitly confirmed."""
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

    # Refusal without unlock flag
    with pytest.raises(PermissionError) as exc_info:
        validate_split_access(split="test", start=start, end=end, unlock_test=False)
    assert "LOCKED" in str(exc_info.value)

    # Refusal with unlock flag but incorrect confirmation
    with pytest.raises(PermissionError) as exc_info2:
        validate_split_access(
            split="test", start=start, end=end, unlock_test=True, confirm_text="NO"
        )
    assert "CONFIRM" in str(exc_info2.value)

    # Success with unlock flag and CONFIRM
    validate_split_access(
        split="test", start=start, end=end, unlock_test=True, confirm_text="CONFIRM"
    )


def test_funding_applied_at_actual_timestamps_non_8h(tmp_path: Path):
    """Confirm funding is applied at ACTUAL parquet timestamps, not an assumed 8h schedule.

    Includes a scenario where funding occurs at non-8h intervals (15m and 1h), and
    confirms no funding is applied when not present in the parquet dataset.
    """
    t0 = 1704067200000  # 2024-01-01 00:00:00
    # Generate 15 consecutive 5m bars (75 minutes total, spanning NO 8h boundary)
    klines = []
    for i in range(15):
        bt_open = t0 + (i * 300_000)
        bt_close = bt_open + 300_000
        klines.append(
            {
                "open_time": bt_open,
                "close_time": bt_close,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
            }
        )

    # Two funding events in Parquet at NON-8h intervals:
    # 1. At t3 = t0 + 15m (300,000 * 3) -> 15m interval, rate = +0.005 (+0.5%)
    #    Add a sub-second jitter of 250ms to verify enclosing 5m bar bucketing
    # 2. At t12 = t0 + 60m (300,000 * 12) -> 1h interval, rate = -0.002 (-0.2%)
    t3_jitter = t0 + (3 * 300_000) + 250
    t12_exact = t0 + (12 * 300_000)
    funding_events = [
        (t3_jitter, 0.005),
        (t12_exact, -0.002),
    ]

    _setup_symbol_data(tmp_path, "BTCUSDT", klines, funding=funding_events)

    open_intent = Intent(
        symbol="BTCUSDT",
        action="open_long",
        order_type="market",
        stop_loss=Decimal("80.0"),
        take_profit=Decimal("150.0"),
    )
    close_intent = Intent(symbol="BTCUSDT", action="close")

    t_open_order = t0 + 300_000
    t_close_order = t0 + (13 * 300_000)
    strategy = ScriptedStrategy(
        {
            t_open_order: [open_intent],
            t_close_order: [close_intent],
        }
    )

    engine = BacktestEngine(
        strategy=strategy,
        symbols=["BTCUSDT"],
        data_dir=tmp_path,
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
        initial_cash=Decimal("10000.0"),
        maker_fee_rate=Decimal("0.0"),
        taker_fee_rate=Decimal("0.0"),
        slippage_bps=Decimal("0.0"),
        slippage_atr_factor=Decimal("0.0"),
        sizer=FixedFractionalSizer(risk_pct=Decimal("0.01")),
    )
    res = engine.run()

    assert len(res.trades) == 1
    tr = res.trades[0]

    # Position size: risk = 10000 * 0.01 = 100. Stop distance = 100 - 80 = 20. Qty = 5.0
    # Notional at bar 3 (open=100.0): 100.0 * 5.0 = 500.0.
    # Event 1 (15m, rate=+0.005): Long pays 500 * 0.005 = 2.50
    # Event 2 (1h, rate=-0.002): Long receives 500 * (-0.002) = -1.00 (i.e. +1.00 credit)
    # Net funding paid = 2.50 - 1.00 = 1.50
    assert tr.funding_paid == Decimal("1.50")
    assert res.total_funding_paid == Decimal("1.50")
