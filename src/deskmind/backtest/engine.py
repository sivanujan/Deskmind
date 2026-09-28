"""Trustworthy, event-driven backtest engine for Binance USDT-M perpetuals.

Architecture & Execution Realism:
- Base clock: 5m bars. Higher timeframes become visible only after they close
  (same bar_close semantics as live).
- Incremental agents: reuses the exact same SymbolTrendAgent as live; no look-ahead.
- Market entries: fill at the NEXT bar's open + slippage (never at signal bar close).
- Limit entries: fill only if price trades THROUGH the limit (not merely touches).
- Stop-loss: exchange-side stop-market; fills at stop + slippage, or bar open if gapped through.
- Same-bar SL & TP: assumes SL hit first (conservative); tracked in metrics.
- Fees: maker/taker rates (Binance VIP0 defaults).
- Slippage: fixed bps + ATR fraction model.
- Funding: applied on open position notional at actual historical funding timestamps.
- Liquidation: evaluated against 5m MARK price klines using maintenance margin brackets.
- Exchange filters: tickSize, stepSize rounding; minQty, minNotional enforcement.
- Gaps: flagged; no fills inside gaps.
- Max ONE open position across all symbols at any time (Phase 1 rule).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from deskmind.agents.trend.agent import SymbolTrendAgent
from deskmind.agents.trend.models import Bar
from deskmind.backtest.fills import calc_slippage_pct, simulate_candle_exit
from deskmind.backtest.filters import SymbolFilters, load_symbol_filters, round_price
from deskmind.backtest.liquidation import compute_liquidation_price, is_liquidated
from deskmind.backtest.sizing import PositionSizer
from deskmind.config import (
    DEFAULT_INITIAL_CASH,
    DEFAULT_MAKER_FEE_RATE,
    DEFAULT_SLIPPAGE_ATR_FACTOR,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_TAKER_FEE_RATE,
)
from deskmind.data.datasets import TIMEFRAME_MS
from deskmind.data.storage import Dataset, Store, duckdb_connect, load_klines
from deskmind.risk import CircuitBreakerTracker, RiskConfig, RiskManagerSizer
from deskmind.strategies.base import (
    AccountSnapshot,
    BarSnapshot,
    BaseStrategy,
    PositionSnapshot,
    StrategyContext,
)
from deskmind.universe.history import MembershipHistory

logger = logging.getLogger("deskmind.backtest")


@dataclass
class Trade:
    id: str
    symbol: str
    side: Literal["long", "short"]
    entry_time_ms: int
    entry_price: Decimal
    exit_time_ms: int
    exit_price: Decimal
    qty: Decimal
    pnl: Decimal
    pnl_pct: Decimal
    r_multiple: Decimal
    exit_reason: Literal[
        "take_profit", "stop_loss", "liquidation", "sl_tp_same_bar", "manual_close", "delisted"
    ]
    fees_paid: Decimal
    funding_paid: Decimal
    slippage_paid: Decimal
    holding_time_minutes: int
    spanned_gap: bool
    initial_risk: Decimal
    risk_decision: dict[str, Any] | None = None


@dataclass
class ActivePosition:
    symbol: str
    side: Literal["long", "short"]
    entry_price: Decimal
    qty: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    entry_time_ms: int
    initial_risk: Decimal
    liquidation_price: Decimal
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")
    slippage_paid: Decimal = Decimal("0")
    spanned_gap: bool = False
    risk_decision: dict[str, Any] | None = None


@dataclass
class RejectedIntent:
    symbol: str
    action: str
    price: Decimal
    timestamp_ms: int
    reasons: list[str]
    decision: dict[str, Any] = field(default_factory=dict)


@dataclass
class PendingOrder:
    symbol: str
    action: Literal["open_long", "open_short", "close"]
    order_type: Literal["market", "limit"]
    qty: Decimal
    limit_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal
    initial_risk: Decimal
    intent_time_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EquityPoint:
    time_ms: int
    equity: Decimal
    cash: Decimal
    position_symbol: str | None = None
    unrealized_pnl: Decimal = Decimal("0")


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    same_bar_sl_tp_count: int
    liquidations_count: int
    skipped_signals_count: int
    position_open_skips: int
    duplicate_setup_skips: int
    total_funding_paid: Decimal
    total_fees_paid: Decimal
    total_slippage_paid: Decimal
    start_time_ms: int
    end_time_ms: int
    initial_cash: Decimal
    final_equity: Decimal
    rejected_intents: list[RejectedIntent] = field(default_factory=list)
    circuit_breaker_transitions: list[dict[str, Any]] = field(default_factory=list)
    raw_entry_signals: int = 0
    intent_distribution: dict[int, int] = field(default_factory=dict)
    is_shadow_mode: bool = False
    universe_skips: int = 0  # entry intents on symbols outside that day's universe
    delisted_exits: int = 0
    universe_filter: str = "none"  # "point_in_time" when a MembershipHistory was used


def load_funding_map(data_dir: Path, symbol: str) -> dict[int, Decimal]:
    """Map funding_time (epoch ms) -> funding_rate (Decimal).

    Historical funding rates from Parquet are bucketed into the enclosing 5m bar
    timestamp [bar_t, bar_t + 300_000) to ensure exact alignment with the 5m engine
    clock, regardless of non-8h intervals (1h, 2h, 4h) or sub-second timestamp jitter.
    """
    store = Store(data_dir, symbol)
    ds = Dataset("funding")
    if not store.files(ds):
        return {}
    con = duckdb_connect()
    try:
        query = (
            "SELECT epoch_ms(funding_time), funding_rate "
            f"FROM read_parquet('{store.glob(ds)}') ORDER BY funding_time"
        )
        rows = con.execute(query).fetchall()
    finally:
        con.close()
    res: dict[int, Decimal] = {}
    for t, r in rows:
        if r is not None:
            # Map funding timestamp to the 5m bar interval [bar_t, bar_t + 300_000)
            bar_t = (int(t) // 300_000) * 300_000
            res[bar_t] = res.get(bar_t, Decimal("0")) + Decimal(str(r))
    return res


class BacktestEngine:
    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: list[str],
        data_dir: Path,
        start: datetime,
        end: datetime,
        initial_cash: Decimal = DEFAULT_INITIAL_CASH,
        maker_fee_rate: Decimal = DEFAULT_MAKER_FEE_RATE,
        taker_fee_rate: Decimal = DEFAULT_TAKER_FEE_RATE,
        slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS,
        slippage_atr_factor: Decimal = DEFAULT_SLIPPAGE_ATR_FACTOR,
        sizer: PositionSizer | None = None,
        leverage: int = 5,
        warmup_days: int = 0,
        liquidation_safety_buffer_pct: Decimal = Decimal("0.005"),
        universe: MembershipHistory | None = None,
    ) -> None:
        self.strategy = strategy
        # Point-in-time universe: entries only on symbols that were tradeable members that day.
        self.universe = universe
        self.universe_skips = 0
        self.delisted_exits = 0
        self.symbols = list(symbols)
        self.data_dir = data_dir
        self.start = start
        self.end = end
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.maker_fee_rate = maker_fee_rate
        self.taker_fee_rate = taker_fee_rate
        self.slippage_bps = slippage_bps
        self.slippage_atr_factor = slippage_atr_factor
        if sizer is not None:
            self.sizer = sizer
        else:
            risk_cfg = RiskConfig(max_leverage=leverage)
            tracker = CircuitBreakerTracker(config=risk_cfg, initial_equity=initial_cash)
            self.sizer = RiskManagerSizer(config=risk_cfg, tracker=tracker)
        self.leverage = leverage
        self.warmup_days = warmup_days
        self.liquidation_safety_buffer_pct = liquidation_safety_buffer_pct
        warmup_ms = warmup_days * 86_400 * 1000
        self.trading_start_ms = int(start.timestamp() * 1000) + warmup_ms

        self.filters: dict[str, SymbolFilters] = {
            s: load_symbol_filters(data_dir, s) for s in self.symbols
        }
        self.agents: dict[str, SymbolTrendAgent] = {
            s: SymbolTrendAgent(s, ["5m", "15m", "1h", "2h"]) for s in self.symbols
        }

        self.active_position: ActivePosition | None = None
        self.pending_order: PendingOrder | None = None
        self.trades: list[Trade] = []
        self.rejected_intents: list[RejectedIntent] = []
        self.equity_curve: list[EquityPoint] = []

        self.same_bar_sl_tp_count = 0
        self.liquidations_count = 0
        self.skipped_signals_count = 0
        self.position_open_skips = 0  # Entry intents discarded due to active position/order
        self.duplicate_setup_skips = 0  # Entry intents discarded as already-seen setup_id
        self.raw_entry_signals = 0  # Total raw entry intents emitted across all symbols
        self.intent_distribution: dict[int, int] = {}  # Count of bars with N entry intents
        # Tracks setup_ids that have been evaluated (approved or RM-rejected) this session.
        # An empty setup_id string is never deduplicated.
        self.seen_setup_ids: set[str] = set()
        self.total_funding_paid = Decimal("0")
        self.total_fees_paid = Decimal("0")
        self.total_slippage_paid = Decimal("0")

    def _calc_slippage_pct(self, price: Decimal, atr: float | None) -> Decimal:
        """Slippage = fixed_bps + atr_factor * (atr / price)."""
        return calc_slippage_pct(price, atr, self.slippage_bps, self.slippage_atr_factor)

    def _settle_delisted(self, price: Decimal, t_ms: int) -> None:
        """Close the open position at the delisting settlement price (taker fee, no slippage)."""
        pos = self.active_position
        assert pos is not None
        fee = price * pos.qty * self.taker_fee_rate
        self.total_fees_paid += fee
        gross = (price - pos.entry_price) * pos.qty
        if pos.side == "short":
            gross = -gross
        self.cash += gross - fee
        net = gross - (pos.fees_paid + fee) - pos.funding_paid
        notional = pos.entry_price * pos.qty
        self.trades.append(
            Trade(
                id=f"trade_{len(self.trades) + 1}",
                symbol=pos.symbol,
                side=pos.side,
                entry_time_ms=pos.entry_time_ms,
                entry_price=pos.entry_price,
                exit_time_ms=t_ms,
                exit_price=price,
                qty=pos.qty,
                pnl=net,
                pnl_pct=(net / notional * Decimal("100")) if notional > 0 else Decimal("0"),
                r_multiple=net / pos.initial_risk if pos.initial_risk > 0 else Decimal("0"),
                exit_reason="delisted",
                fees_paid=pos.fees_paid + fee,
                funding_paid=pos.funding_paid,
                slippage_paid=pos.slippage_paid,
                holding_time_minutes=max(1, (t_ms - pos.entry_time_ms) // 60_000),
                spanned_gap=pos.spanned_gap,
                initial_risk=pos.initial_risk,
                risk_decision=pos.risk_decision,
            )
        )
        if isinstance(self.sizer, RiskManagerSizer):
            self.sizer.tracker.record_trade_exit(net, t_ms)
        self.delisted_exits += 1
        self.active_position = None

    def run(self) -> BacktestResult:
        logger.info("Loading market data for %s from %s to %s", self.symbols, self.start, self.end)

        # 1. Load klines and mark klines
        klines_5m: dict[str, dict[int, Any]] = {}
        mark_5m: dict[str, dict[int, Any]] = {}
        higher_tf_klines: dict[str, dict[str, dict[int, Bar]]] = {
            s: {"15m": {}, "1h": {}, "2h": {}} for s in self.symbols
        }
        funding_map: dict[str, dict[int, Decimal]] = {}

        for sym in self.symbols:
            funding_map[sym] = load_funding_map(self.data_dir, sym)

            # Load 5m klines
            tbl_5m = load_klines(self.data_dir, sym, "5m", self.start, self.end)
            if tbl_5m.num_rows > 0:
                pydict = tbl_5m.to_pydict()
                klines_5m[sym] = {
                    int(pydict["open_time"][i].timestamp() * 1000): {
                        "open_time": int(pydict["open_time"][i].timestamp() * 1000),
                        "close_time": int(pydict["close_time"][i].timestamp() * 1000),
                        "open": pydict["open"][i],
                        "high": pydict["high"][i],
                        "low": pydict["low"][i],
                        "close": pydict["close"][i],
                        "volume": pydict["volume"][i],
                    }
                    for i in range(tbl_5m.num_rows)
                }
            else:
                klines_5m[sym] = {}

            # Load 5m mark klines
            tbl_mark = load_klines(self.data_dir, sym, "5m", self.start, self.end, mark=True)
            if tbl_mark.num_rows > 0:
                mdict = tbl_mark.to_pydict()
                mark_5m[sym] = {
                    int(mdict["open_time"][i].timestamp() * 1000): {
                        "open_time": int(mdict["open_time"][i].timestamp() * 1000),
                        "high": mdict["high"][i],
                        "low": mdict["low"][i],
                        "close": mdict["close"][i],
                    }
                    for i in range(tbl_mark.num_rows)
                }
            else:
                mark_5m[sym] = {}

            # Load 15m, 1h, 2h klines for agent feeding
            for tf in ("15m", "1h", "2h"):
                tbl_htf = load_klines(self.data_dir, sym, tf, self.start, self.end)
                if tbl_htf.num_rows > 0:
                    hdict = tbl_htf.to_pydict()
                    higher_tf_klines[sym][tf] = {}
                    for i in range(tbl_htf.num_rows):
                        o_ms = int(hdict["open_time"][i].timestamp() * 1000)
                        boundary_ms = o_ms + TIMEFRAME_MS[tf]
                        higher_tf_klines[sym][tf][boundary_ms] = Bar(
                            open_time=o_ms,
                            close_time=boundary_ms,
                            open=hdict["open"][i],
                            high=hdict["high"][i],
                            low=hdict["low"][i],
                            close=hdict["close"][i],
                        )

        # Delisting: an open position is settled at the last traded price before delisting.
        settle: dict[str, tuple[int, Decimal]] = {}
        if self.universe is not None:
            for sym, delisted_ms in self.universe.delisted_at_ms.items():
                bars = klines_5m.get(sym) or {}
                before = [t for t in bars if t < delisted_ms]
                if before:
                    last = max(before)
                    settle[sym] = (
                        min(delisted_ms, last + 300_000),
                        Decimal(str(bars[last]["close"])),
                    )

        # 2. Build sorted unified 5m timeline of open_times
        all_timestamps = sorted(set(t for sym in self.symbols for t in klines_5m.get(sym, {})))
        if not all_timestamps:
            return BacktestResult(
                trades=[],
                equity_curve=[],
                same_bar_sl_tp_count=0,
                liquidations_count=0,
                skipped_signals_count=0,
                position_open_skips=0,
                duplicate_setup_skips=0,
                total_funding_paid=Decimal("0"),
                total_fees_paid=Decimal("0"),
                total_slippage_paid=Decimal("0"),
                start_time_ms=int(self.start.timestamp() * 1000),
                end_time_ms=int(self.end.timestamp() * 1000),
                initial_cash=self.initial_cash,
                final_equity=self.initial_cash,
            )

        step_5m = 300_000
        prev_time = all_timestamps[0] - step_5m

        # 3. Main Event Loop: Step through 5m bars
        for t_open in all_timestamps:
            t_close = t_open + step_5m
            is_gap = (t_open - prev_time) > step_5m
            prev_time = t_open

            # Check if active position spans a gap
            if self.active_position is not None and is_gap:
                self.active_position.spanned_gap = True

            # ----------------------------------------------------
            # STEP A: Execute pending entry orders at bar open
            # ----------------------------------------------------
            if self.pending_order is not None:
                p_order = self.pending_order
                self.pending_order = None
                sym = p_order.symbol
                bar_dict = klines_5m.get(sym, {}).get(t_open)

                if bar_dict is not None and not is_gap:
                    b_open = Decimal(str(bar_dict["open"]))
                    b_low = Decimal(str(bar_dict["low"]))
                    b_high = Decimal(str(bar_dict["high"]))
                    filters = self.filters[sym]

                    # Slippage model based on 15m/1h ATR
                    atr_val = (
                        self.agents[sym].engines["15m"].atr or self.agents[sym].engines["5m"].atr
                    )
                    slip_pct = self._calc_slippage_pct(b_open, atr_val)

                    fill_price: Decimal | None = None
                    is_maker = False

                    if p_order.action == "close" and self.active_position is not None:
                        pos = self.active_position
                        if pos.side == "long":
                            raw_fill = b_open * (Decimal("1") - slip_pct)
                            fill_price = round_price(raw_fill, filters.tick_size)
                            gross_pnl = (fill_price - pos.entry_price) * pos.qty
                            exit_slip = max(Decimal("0"), (b_open - fill_price) * pos.qty)
                        else:
                            raw_fill = b_open * (Decimal("1") + slip_pct)
                            fill_price = round_price(raw_fill, filters.tick_size)
                            gross_pnl = (pos.entry_price - fill_price) * pos.qty
                            exit_slip = max(Decimal("0"), (fill_price - b_open) * pos.qty)

                        pos.slippage_paid += exit_slip
                        self.total_slippage_paid += exit_slip

                        exit_fee = fill_price * pos.qty * self.taker_fee_rate
                        self.total_fees_paid += exit_fee
                        self.cash += gross_pnl - exit_fee
                        net_pnl = gross_pnl - (pos.fees_paid + exit_fee) - pos.funding_paid
                        r_mult = (
                            net_pnl / pos.initial_risk
                            if pos.initial_risk > Decimal("0")
                            else Decimal("0")
                        )
                        notional_entry = pos.entry_price * pos.qty
                        pnl_pct = (
                            (net_pnl / notional_entry) * Decimal("100")
                            if notional_entry > 0
                            else Decimal("0")
                        )

                        self.trades.append(
                            Trade(
                                id=f"trade_{len(self.trades) + 1}",
                                symbol=pos.symbol,
                                side=pos.side,
                                entry_time_ms=pos.entry_time_ms,
                                entry_price=pos.entry_price,
                                exit_time_ms=t_open,
                                exit_price=fill_price,
                                qty=pos.qty,
                                pnl=net_pnl,
                                pnl_pct=pnl_pct,
                                r_multiple=r_mult,
                                exit_reason="manual_close",
                                fees_paid=pos.fees_paid + exit_fee,
                                funding_paid=pos.funding_paid,
                                slippage_paid=pos.slippage_paid,
                                holding_time_minutes=max(1, (t_open - pos.entry_time_ms) // 60_000),
                                spanned_gap=pos.spanned_gap,
                                initial_risk=pos.initial_risk,
                                risk_decision=pos.risk_decision,
                            )
                        )
                        if isinstance(self.sizer, RiskManagerSizer):
                            self.sizer.tracker.record_trade_exit(net_pnl, t_open)
                        self.active_position = None

                    elif p_order.action in ("open_long", "open_short"):
                        if p_order.order_type == "market":
                            if p_order.action == "open_long":
                                raw_fill = b_open * (Decimal("1") + slip_pct)
                                fill_price = round_price(raw_fill, filters.tick_size)
                                slip_paid = (fill_price - b_open) * p_order.qty
                            else:
                                raw_fill = b_open * (Decimal("1") - slip_pct)
                                fill_price = round_price(raw_fill, filters.tick_size)
                                slip_paid = (b_open - fill_price) * p_order.qty
                            self.total_slippage_paid += max(Decimal("0"), slip_paid)
                        elif p_order.order_type == "limit" and p_order.limit_price is not None:
                            # Trade THROUGH check (not just touches)
                            if (p_order.action == "open_long" and b_low < p_order.limit_price) or (
                                p_order.action == "open_short" and b_high > p_order.limit_price
                            ):
                                fill_price = p_order.limit_price
                                is_maker = True

                        if fill_price is not None:
                            # INVARIANT (a): Every open position must have an active stop
                            if p_order.stop_loss is None or p_order.stop_loss <= Decimal("0"):
                                raise RuntimeError(
                                    f"Invariant violation: Order for {sym} missing active stop "
                                    f"loss ({p_order.stop_loss})"
                                )

                            fee_rate = self.maker_fee_rate if is_maker else self.taker_fee_rate
                            fee = fill_price * p_order.qty * fee_rate
                            self.total_fees_paid += fee
                            self.cash -= fee

                            side: Literal["long", "short"] = (
                                "long" if p_order.action == "open_long" else "short"
                            )
                            order_leverage = int(
                                p_order.metadata.get("risk_decision", {}).get(
                                    "leverage", self.leverage
                                )
                            )
                            liq_price = compute_liquidation_price(
                                side=side,
                                entry_price=fill_price,
                                leverage=order_leverage,
                                qty=p_order.qty,
                            )

                            # INVARIANT (b): Stop strictly inside liquidation with safety buffer
                            buf = self.liquidation_safety_buffer_pct
                            if side == "long":
                                min_stop = liq_price * (Decimal("1") + buf)
                                if p_order.stop_loss <= min_stop:
                                    raise RuntimeError(
                                        f"Invariant violation: Long stop loss {p_order.stop_loss} "
                                        f"is outside or violates safety buffer from liquidation "
                                        f"price {liq_price} (min safe stop: {min_stop})"
                                    )
                            else:
                                max_stop = liq_price * (Decimal("1") - buf)
                                if p_order.stop_loss >= max_stop:
                                    raise RuntimeError(
                                        f"Invariant violation: Short stop loss {p_order.stop_loss} "
                                        f"is outside or violates safety buffer from liquidation "
                                        f"price {liq_price} (max safe stop: {max_stop})"
                                    )

                            self.active_position = ActivePosition(
                                symbol=sym,
                                side=side,
                                entry_price=fill_price,
                                qty=p_order.qty,
                                stop_loss=p_order.stop_loss,
                                take_profit=p_order.take_profit,
                                entry_time_ms=t_open,
                                initial_risk=p_order.initial_risk,
                                liquidation_price=liq_price,
                                fees_paid=fee,
                                slippage_paid=max(Decimal("0"), slip_paid)
                                if p_order.order_type == "market"
                                else Decimal("0"),
                                spanned_gap=is_gap,
                                risk_decision=p_order.metadata.get("risk_decision"),
                            )
                        elif p_order.order_type == "limit":
                            # Retain limit order on book until filled or canceled
                            self.pending_order = p_order

            # ----------------------------------------------------
            # STEP A2: Delisting settles an open position. (A pending entry on a delisted
            # symbol never fills: STEP A drops it when its symbol has no bar.)
            # ----------------------------------------------------
            if (
                self.active_position is not None
                and self.active_position.symbol in settle
                and t_open >= settle[self.active_position.symbol][0]
            ):
                self._settle_delisted(settle[self.active_position.symbol][1], t_open)

            # ----------------------------------------------------
            # STEP B: Manage Active Position on this bar
            # ----------------------------------------------------
            if self.active_position is not None:
                pos = self.active_position
                sym = pos.symbol

                # INVARIANT (a): Active position must have a valid stop loss at all times
                if pos.stop_loss is None or pos.stop_loss <= Decimal("0"):
                    raise RuntimeError(
                        f"Invariant violation: Active position for {sym} missing valid stop "
                        f"loss ({pos.stop_loss})"
                    )

                bar_dict = klines_5m.get(sym, {}).get(t_open)
                mark_dict = mark_5m.get(sym, {}).get(t_open)

                if bar_dict is not None:
                    # INVARIANT (d): Stops are checked on every 5m bar of the position's OWN symbol
                    assert sym == pos.symbol

                    b_open = Decimal(str(bar_dict["open"]))
                    b_high = Decimal(str(bar_dict["high"]))
                    b_low = Decimal(str(bar_dict["low"]))
                    filters = self.filters[sym]

                    # 1. Funding check: at funding timestamps on open position
                    funding_rates = funding_map.get(sym, {})
                    if t_open in funding_rates:
                        f_rate = funding_rates[t_open]
                        notional = b_open * pos.qty
                        # Long pays positive funding, receives negative funding
                        # Short receives positive funding, pays negative funding
                        payment = notional * f_rate if pos.side == "long" else -notional * f_rate
                        self.cash -= payment
                        pos.funding_paid += payment
                        self.total_funding_paid += payment

                    # 2. Check for Gap Liquidation at open (bar opened beyond liquidation price)
                    is_gap_liq = (pos.side == "long" and b_open <= pos.liquidation_price) or (
                        pos.side == "short" and b_open >= pos.liquidation_price
                    )

                    atr_val = (
                        self.agents[sym].engines["15m"].atr or self.agents[sym].engines["5m"].atr
                    )
                    slip_pct = self._calc_slippage_pct(b_open, atr_val)

                    exit_price: Decimal | None = None
                    exit_reason: (
                        Literal["take_profit", "stop_loss", "sl_tp_same_bar", "liquidation"] | None
                    ) = None
                    is_maker = False

                    if is_gap_liq:
                        exit_price = b_open
                        exit_reason = "liquidation"
                    else:
                        cand_exit_price, cand_exit_reason, cand_is_maker, cand_slip_pct = (
                            simulate_candle_exit(
                                pos_side=pos.side,
                                stop_loss=pos.stop_loss,
                                take_profit=pos.take_profit,
                                b_open=b_open,
                                b_high=b_high,
                                b_low=b_low,
                                tick_size=filters.tick_size,
                                atr=atr_val,
                                slippage_bps=self.slippage_bps,
                                slippage_atr_factor=self.slippage_atr_factor,
                            )
                        )
                        if cand_exit_reason is not None:
                            exit_price = cand_exit_price
                            exit_reason = cand_exit_reason
                            is_maker = cand_is_maker
                            slip_pct = cand_slip_pct
                            if exit_reason == "sl_tp_same_bar":
                                self.same_bar_sl_tp_count += 1

                        # 4. If neither SL nor TP triggered, check for mark price liq divergence
                        if exit_reason is None:
                            mark_h = Decimal(str(mark_dict["high"])) if mark_dict else b_high
                            mark_lo = Decimal(str(mark_dict["low"])) if mark_dict else b_low
                            if is_liquidated(pos.side, pos.liquidation_price, mark_h, mark_lo):
                                exit_price = pos.liquidation_price
                                exit_reason = "liquidation"

                    if exit_price is not None and exit_reason is not None:
                        if exit_reason == "liquidation":
                            # INVARIANT (c): liquidation exit records full isolated margin + fees
                            self.liquidations_count += 1
                            pos_leverage = int(
                                pos.risk_decision.get("leverage", self.leverage)
                                if pos.risk_decision
                                else self.leverage
                            )
                            isolated_margin = (pos.entry_price * pos.qty) / Decimal(
                                str(pos_leverage)
                            )
                            liq_fee = (pos.entry_price * pos.qty) * filters.liquidation_fee
                            total_liq_loss = isolated_margin + liq_fee

                            self.cash -= total_liq_loss
                            self.total_fees_paid += liq_fee

                            r_mult = (
                                -total_liq_loss / pos.initial_risk
                                if pos.initial_risk > Decimal("0")
                                else Decimal("-1.0")
                            )

                            self.trades.append(
                                Trade(
                                    id=f"trade_{len(self.trades) + 1}",
                                    symbol=pos.symbol,
                                    side=pos.side,
                                    entry_time_ms=pos.entry_time_ms,
                                    entry_price=pos.entry_price,
                                    exit_time_ms=t_open,
                                    exit_price=exit_price or pos.liquidation_price,
                                    qty=pos.qty,
                                    pnl=-total_liq_loss,
                                    pnl_pct=Decimal("-100.0"),
                                    r_multiple=r_mult,
                                    exit_reason="liquidation",
                                    fees_paid=pos.fees_paid + liq_fee,
                                    funding_paid=pos.funding_paid,
                                    slippage_paid=pos.slippage_paid,
                                    holding_time_minutes=max(
                                        1, (t_open - pos.entry_time_ms) // 60_000
                                    ),
                                    spanned_gap=pos.spanned_gap,
                                    initial_risk=pos.initial_risk,
                                    risk_decision=pos.risk_decision,
                                )
                            )
                            if isinstance(self.sizer, RiskManagerSizer):
                                self.sizer.tracker.record_trade_exit(-total_liq_loss, t_open)
                            self.active_position = None
                        else:
                            # Track exit slippage on market/stop fills
                            if exit_reason in ("stop_loss", "sl_tp_same_bar"):
                                ref_stop = (
                                    pos.stop_loss
                                    if b_open > pos.stop_loss and pos.side == "long"
                                    else (
                                        pos.stop_loss
                                        if b_open < pos.stop_loss and pos.side == "short"
                                        else b_open
                                    )
                                )
                                exit_slip = max(
                                    Decimal("0"),
                                    (ref_stop - exit_price) * pos.qty
                                    if pos.side == "long"
                                    else (exit_price - ref_stop) * pos.qty,
                                )
                                pos.slippage_paid += exit_slip
                                self.total_slippage_paid += exit_slip

                            fee_rate = self.maker_fee_rate if is_maker else self.taker_fee_rate
                            exit_fee = exit_price * pos.qty * fee_rate
                            self.total_fees_paid += exit_fee

                            if pos.side == "long":
                                gross_pnl = (exit_price - pos.entry_price) * pos.qty
                            else:
                                gross_pnl = (pos.entry_price - exit_price) * pos.qty

                            net_pnl = gross_pnl - (pos.fees_paid + exit_fee) - pos.funding_paid
                            self.cash += gross_pnl - exit_fee

                            r_mult = (
                                net_pnl / pos.initial_risk
                                if pos.initial_risk > Decimal("0")
                                else Decimal("0")
                            )
                            notional_entry = pos.entry_price * pos.qty
                            pnl_pct = (
                                (net_pnl / notional_entry) * Decimal("100")
                                if notional_entry > 0
                                else Decimal("0")
                            )

                            self.trades.append(
                                Trade(
                                    id=f"trade_{len(self.trades) + 1}",
                                    symbol=pos.symbol,
                                    side=pos.side,
                                    entry_time_ms=pos.entry_time_ms,
                                    entry_price=pos.entry_price,
                                    exit_time_ms=t_open,
                                    exit_price=exit_price,
                                    qty=pos.qty,
                                    pnl=net_pnl,
                                    pnl_pct=pnl_pct,
                                    r_multiple=r_mult,
                                    exit_reason=exit_reason,
                                    fees_paid=pos.fees_paid + exit_fee,
                                    funding_paid=pos.funding_paid,
                                    slippage_paid=pos.slippage_paid,
                                    holding_time_minutes=max(
                                        1, (t_open - pos.entry_time_ms) // 60_000
                                    ),
                                    spanned_gap=pos.spanned_gap,
                                    initial_risk=pos.initial_risk,
                                    risk_decision=pos.risk_decision,
                                )
                            )
                            if isinstance(self.sizer, RiskManagerSizer):
                                self.sizer.tracker.record_trade_exit(net_pnl, t_open)
                            self.active_position = None

            # ----------------------------------------------------
            # STEP C: Feed Incremental Agents at bar close (t_close)
            # ----------------------------------------------------
            contexts: dict[str, StrategyContext] = {}

            for sym in self.symbols:
                bar_dict = klines_5m.get(sym, {}).get(t_open)
                if bar_dict is None:
                    continue

                bar_5m = Bar(
                    t_open,
                    t_close,
                    bar_dict["open"],
                    bar_dict["high"],
                    bar_dict["low"],
                    bar_dict["close"],
                )

                bars_by_tf: dict[str, list[Bar]] = {"5m": [bar_5m]}

                # Check which higher timeframes closed at t_close
                for tf in ("15m", "1h", "2h"):
                    iv = TIMEFRAME_MS[tf]
                    if t_close % iv == 0:
                        htf_bar = higher_tf_klines[sym][tf].get(t_close)
                        if htf_bar:
                            bars_by_tf[tf] = [htf_bar]

                # Update the agent incrementally
                self.agents[sym].on_bar_close(t_close, bars_by_tf)

                # Prepare Strategy Context
                report = self.agents[sym].report(t_close)

                unrealized_pnl = Decimal("0")
                if self.active_position is not None and self.active_position.symbol == sym:
                    pos = self.active_position
                    c_close = Decimal(str(bar_dict["close"]))
                    if pos.side == "long":
                        unrealized_pnl = (c_close - pos.entry_price) * pos.qty
                    else:
                        unrealized_pnl = (pos.entry_price - c_close) * pos.qty

                cur_equity = self.cash + unrealized_pnl

                pos_snapshot = (
                    PositionSnapshot(
                        symbol=self.active_position.symbol,
                        side=self.active_position.side,
                        entry_price=self.active_position.entry_price,
                        qty=self.active_position.qty,
                        stop_loss=self.active_position.stop_loss,
                        take_profit=self.active_position.take_profit,
                        entry_time_ms=self.active_position.entry_time_ms,
                        liquidation_price=self.active_position.liquidation_price,
                        spanned_gap=self.active_position.spanned_gap,
                    )
                    if self.active_position is not None
                    else None
                )

                contexts[sym] = StrategyContext(
                    symbol=sym,
                    as_of_ms=t_close,
                    current_bar=BarSnapshot(
                        t_open,
                        t_close,
                        Decimal(str(bar_dict["open"])),
                        Decimal(str(bar_dict["high"])),
                        Decimal(str(bar_dict["low"])),
                        Decimal(str(bar_dict["close"])),
                        Decimal(str(bar_dict["volume"])),
                    ),
                    agent_report=report,
                    account=AccountSnapshot(
                        cash=self.cash,
                        equity=cur_equity,
                        realized_pnl=sum((t.pnl for t in self.trades), Decimal("0")),
                        unrealized_pnl=unrealized_pnl,
                        open_position=pos_snapshot,
                    ),
                )

            # ----------------------------------------------------
            # STEP D: Strategy Decision & Signal Ranking
            # ----------------------------------------------------
            # Only generate strategy intents if past the warm-up period
            if contexts and t_close >= self.trading_start_ms:
                raw_intents = self.strategy.on_bar(contexts)
                all_entries = [it for it in raw_intents if it.action in ("open_long", "open_short")]
                self.raw_entry_signals += len(all_entries)
                num_entries = len(all_entries)
                self.intent_distribution[num_entries] = (
                    self.intent_distribution.get(num_entries, 0) + 1
                )

                if self.active_position is not None:
                    for it in raw_intents:
                        if it.symbol == self.active_position.symbol:
                            if it.action == "move_stop":
                                if it.stop_loss is None or it.stop_loss <= Decimal("0"):
                                    raise RuntimeError(
                                        f"Invariant violation: Cannot move stop to invalid "
                                        f"value ({it.stop_loss})"
                                    )
                                self.active_position.stop_loss = it.stop_loss
                            elif it.action == "close":
                                self.pending_order = PendingOrder(
                                    symbol=self.active_position.symbol,
                                    action="close",
                                    order_type="market",
                                    qty=self.active_position.qty,
                                    limit_price=None,
                                    stop_loss=Decimal("0"),
                                    take_profit=Decimal("0"),
                                    initial_risk=self.active_position.initial_risk,
                                    intent_time_ms=t_close,
                                )
                                break
                    # Count entry intents for other symbols that are silently discarded
                    # because we already have an active position (Phase 1: max 1 position).
                    other_entry_intents = [
                        it
                        for it in raw_intents
                        if it.action in ("open_long", "open_short")
                        and it.symbol != self.active_position.symbol
                    ]
                    self.position_open_skips += len(other_entry_intents)
                elif self.pending_order is None:
                    entry_intents = [
                        it for it in raw_intents if it.action in ("open_long", "open_short")
                    ]
                    if self.universe is not None and entry_intents:
                        members = self.universe.get_universe(
                            datetime.fromtimestamp(t_close / 1000, UTC).date()
                        )
                        allowed = [it for it in entry_intents if it.symbol in members]
                        self.universe_skips += len(entry_intents) - len(allowed)
                        entry_intents = allowed

                    if entry_intents:
                        # Deterministic ranking: higher ranking_score first,
                        # symbol alphabetical as tie-breaker
                        ranked_intents = sorted(
                            entry_intents,
                            key=lambda x: (x.ranking_score, x.symbol),
                            reverse=True,
                        )

                        # Deduplicate by setup_id: skip any intent whose setup_id has
                        # already been evaluated this run (approved or RM-rejected).
                        # An empty setup_id is never deduplicated.
                        deduped_intents = []
                        for it in ranked_intents:
                            if it.setup_id and it.setup_id in self.seen_setup_ids:
                                self.duplicate_setup_skips += 1
                            else:
                                deduped_intents.append(it)

                        if not deduped_intents:
                            # All ranked intents were duplicates; nothing to evaluate
                            pass
                        else:
                            chosen = deduped_intents[0]
                            # INVARIANT (a): Every open position/order must have an active stop
                            if chosen.stop_loss is None or chosen.stop_loss <= Decimal("0"):
                                raise RuntimeError(
                                    f"Invariant violation: Strategy emitted {chosen.action} on "
                                    f"{chosen.symbol} missing active stop loss ({chosen.stop_loss})"
                                )

                            # Log lower-ranked intents skipped this bar
                            if len(deduped_intents) > 1:
                                self.skipped_signals_count += len(deduped_intents) - 1

                            ctx = contexts[chosen.symbol]
                            cur_close = ctx.current_bar.close

                            # Collect rich market context for RiskManager
                            mark_bar = mark_5m.get(chosen.symbol, {}).get(t_open)
                            mark_price = Decimal(str(mark_bar["close"])) if mark_bar else cur_close
                            atr_map: dict[str, Decimal] = {}
                            agent = self.agents.get(chosen.symbol)
                            if agent:
                                for tf in ("5m", "15m", "1h", "2h"):
                                    engine_tf = agent.engines.get(tf)
                                    if engine_tf and engine_tf.atr is not None:
                                        atr_map[tf] = Decimal(str(engine_tf.atr))

                            f_rates = funding_map.get(chosen.symbol, {})
                            cur_f_rate = f_rates.get(t_open)
                            future_fundings = [ft for ft in f_rates if ft > t_close]
                            next_funding_ms = min(future_fundings) if future_fundings else None

                            # Size position
                            if isinstance(self.sizer, RiskManagerSizer):
                                qty, reason = self.sizer.size_position(
                                    ctx.account,
                                    chosen,
                                    cur_close,
                                    self.filters[chosen.symbol],
                                    mark_price=mark_price,
                                    atr_by_tf=atr_map,
                                    next_funding_time_ms=next_funding_ms,
                                    current_funding_rate=cur_f_rate,
                                    timestamp_ms=t_close,
                                )
                                decision = self.sizer.last_decision
                            else:
                                qty, reason = self.sizer.size_position(
                                    ctx.account, chosen, cur_close, self.filters[chosen.symbol]
                                )
                                decision = None

                            # Mark this setup_id as evaluated so it won't be re-submitted
                            if chosen.setup_id:
                                self.seen_setup_ids.add(chosen.setup_id)

                            if (
                                qty > Decimal("0")
                                and chosen.stop_loss is not None
                                and chosen.take_profit is not None
                            ):
                                init_risk = (
                                    decision.actual_risk_usd
                                    if decision is not None
                                    else abs(cur_close - chosen.stop_loss) * qty
                                )
                                meta = dict(chosen.metadata)
                                if decision is not None:
                                    meta["risk_decision"] = decision.to_dict()

                                self.pending_order = PendingOrder(
                                    symbol=chosen.symbol,
                                    action=chosen.action,  # type: ignore[arg-type]
                                    order_type=chosen.order_type,
                                    qty=qty,
                                    limit_price=chosen.limit_price,
                                    stop_loss=chosen.stop_loss,
                                    take_profit=chosen.take_profit,
                                    initial_risk=init_risk,
                                    intent_time_ms=t_close,
                                    metadata=meta,
                                )
                            else:
                                failed_reasons = (
                                    [r.code for r in decision.failed_reasons]
                                    if decision is not None
                                    else [reason]
                                )
                                dec_dict = decision.to_dict() if decision is not None else {}
                                self.rejected_intents.append(
                                    RejectedIntent(
                                        symbol=chosen.symbol,
                                        action=chosen.action,
                                        price=cur_close,
                                        timestamp_ms=t_close,
                                        reasons=failed_reasons,
                                        decision=dec_dict,
                                    )
                                )
                                logger.debug("Order rejected for %s: %s", chosen.symbol, reason)

            # ----------------------------------------------------
            # STEP E: Record Equity Point
            # ----------------------------------------------------
            unrealized_pnl = Decimal("0")
            pos_sym = None
            if self.active_position is not None:
                pos = self.active_position
                pos_sym = pos.symbol
                bar_dict = klines_5m.get(pos.symbol, {}).get(t_open)
                if bar_dict is not None:
                    c_close = Decimal(str(bar_dict["close"]))
                    if pos.side == "long":
                        unrealized_pnl = (c_close - pos.entry_price) * pos.qty
                    else:
                        unrealized_pnl = (pos.entry_price - c_close) * pos.qty

            total_equity = self.cash + unrealized_pnl
            if isinstance(self.sizer, RiskManagerSizer):
                self.sizer.tracker.on_time_and_equity(t_close, total_equity)

            self.equity_curve.append(
                EquityPoint(
                    time_ms=t_close,
                    equity=total_equity,
                    cash=self.cash,
                    position_symbol=pos_sym,
                    unrealized_pnl=unrealized_pnl,
                )
            )

        final_eq = self.equity_curve[-1].equity if self.equity_curve else self.cash
        cb_transitions = []
        is_shadow = False
        if isinstance(self.sizer, RiskManagerSizer):
            is_shadow = not self.sizer.config.enable_circuit_breakers
            cb_transitions = [
                {
                    "timestamp_ms": t.timestamp_ms,
                    "timestamp_iso": t.timestamp_iso,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "reason": t.reason,
                }
                for t in self.sizer.tracker.transitions
            ]

        return BacktestResult(
            trades=self.trades,
            equity_curve=self.equity_curve,
            same_bar_sl_tp_count=self.same_bar_sl_tp_count,
            liquidations_count=self.liquidations_count,
            skipped_signals_count=self.skipped_signals_count,
            position_open_skips=self.position_open_skips,
            duplicate_setup_skips=self.duplicate_setup_skips,
            total_funding_paid=self.total_funding_paid,
            total_fees_paid=self.total_fees_paid,
            total_slippage_paid=self.total_slippage_paid,
            start_time_ms=max(all_timestamps[0], self.trading_start_ms),
            end_time_ms=all_timestamps[-1] + step_5m,
            initial_cash=self.initial_cash,
            final_equity=final_eq,
            rejected_intents=self.rejected_intents,
            circuit_breaker_transitions=cb_transitions,
            raw_entry_signals=self.raw_entry_signals,
            intent_distribution=self.intent_distribution,
            is_shadow_mode=is_shadow,
            universe_skips=self.universe_skips,
            delisted_exits=self.delisted_exits,
            universe_filter="point_in_time" if self.universe is not None else "none",
        )
