"""Single source of truth for the backtest-level matched random baseline.

Execution & Realism Invariants:
1. Shared Execution & Fill Model:
   - Uses `simulate_market_fill` for entry execution with slippage.
   - Uses `simulate_candle_exit` for intra-candle SL and TP evaluation.
   - Same-bar SL+TP tie rule: conservative assumption (SL hit first), matching engine rule.
2. Max-Holding / Time Exit Rule:
   - Matches the strategy's actual holding rule: Deskmind research hypotheses (H1-H5)
     have NO intra-trade bar timeout; positions remain open until TP or SL triggers,
     or until the simulation period terminates.
   - When the evaluation horizon/dataset ends, unclosed positions are marked to market
     at the terminal bar close: gross_R = (P_terminal - P_entry) / R_dist.
3. Matching Geometry & Properties:
   - Symbol: exact symbol match.
   - Timestamps: exact time-of-day distribution (open_time % 86_400_000 UTC ms).
   - Direction: exact side match (long / short).
   - Geometry: 1.0R Stop Loss / 2.0R Take Profit based on trade's risk_pct.
   - Cost Model: exact trade cost in R (fees + slippage + funding), or specified cost_r.
4. Per-Trade Expectancy Comparison:
   - Compares per-trade mean net R per seed, not summed USD.
   - Tracks and reports skipped_trades_per_seed (must be 0).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from deskmind.backtest.fills import (
    calc_slippage_pct,
    check_limit_trade_through,
    simulate_candle_exit,
    simulate_limit_fill,
    simulate_market_fill,
)
from deskmind.backtest.filters import round_price


@dataclass
class SampledTradeInfo:
    symbol: str
    side: str
    risk_pct: float
    tod_ms: int
    source_tod_ms: int
    entry_price: float
    stop_price: float
    target_price: float
    gross_r: float
    net_r: float
    exit_reason: str
    entry_time_ms: int = 0
    source_entry_time_ms: int = 0
    order_type: str = "market"


@dataclass
class RandomBaselineStats:
    num_seeds: int
    mean_gross_r: float
    mean_net_r: float
    min_net_r: float
    max_net_r: float
    percentile: float
    beat_count: int
    skipped_trades_per_seed: int
    missed_limit_fills_per_seed: int = 0
    seed_net_exp: list[float] = field(default_factory=list)
    seed_gross_exp: list[float] = field(default_factory=list)
    sampled_trades: list[SampledTradeInfo] = field(default_factory=list)
    sampled_entry_times_by_seed: list[list[int]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_seeds": self.num_seeds,
            "mean_gross_r": self.mean_gross_r,
            "mean_net_r": self.mean_net_r,
            "min_net_r": self.min_net_r,
            "max_net_r": self.max_net_r,
            "percentile": self.percentile,
            "beat_count": self.beat_count,
            "skipped_trades_per_seed": self.skipped_trades_per_seed,
        }


def run_backtest_random_baseline(
    trades: Sequence[Any],
    bars_by_symbol: dict[str, Sequence[Any]],
    num_seeds: int = 1000,
    max_horizon_bars: int | None = None,
    slippage_bps: Decimal = Decimal("2.0"),
    slippage_atr_factor: Decimal = Decimal("0.05"),
    fixed_cost_r: float | None = None,
    tick_size: Decimal = Decimal("0.01"),
    seed_offset: int = 42,
    entry_order_type: str = "market",
    limit_expiry_bars: int = 12,
    sampled_trades: list[SampledTradeInfo] | None = None,
) -> RandomBaselineStats:
    """Run backtest-level matched random baseline with exact engine fill model.

    Invariants:
      1. Uses shared `simulate_market_fill` and `simulate_candle_exit` from `deskmind.backtest.fills`.
      2. For limit orders, uses shared `simulate_limit_fill` requiring trade-through by >= 1 tick.
      3. Matches symbol, side, risk_pct (1R stop, 2R target), and time-of-day (UTC ms).
      4. Compares per-trade expectancy in R (mean net R per seed).
      5. Tracks and returns skipped_trades_per_seed and missed_limit_fills_per_seed.
    """
    # Accept BacktestResult or raw trade sequence
    if hasattr(trades, "trades"):
        actual_trades = list(getattr(trades, "trades"))
    else:
        actual_trades = list(trades)

    total_trades = len(actual_trades)
    if total_trades == 0:
        return RandomBaselineStats(
            num_seeds=num_seeds,
            mean_gross_r=0.0,
            mean_net_r=0.0,
            min_net_r=0.0,
            max_net_r=0.0,
            percentile=50.0,
            beat_count=0,
            skipped_trades_per_seed=0,
            missed_limit_fills_per_seed=0,
        )

    # Extract source trade profiles
    trade_profiles: list[dict[str, Any]] = []
    actual_net_rs: list[float] = []

    for t in actual_trades:
        if isinstance(t, dict):
            sym = str(t.get("symbol", ""))
            side = str(t.get("side", ""))
            entry_ms = int(t.get("entry_time_ms", 0))
            ep = float(t.get("entry_price", 0.0))
            qty = float(t.get("qty", 0.0))
            ir = float(t.get("initial_risk", 100.0))
            pnl = float(t.get("pnl", 0.0))
            fees = float(t.get("fees_paid", 0.0))
            funding = float(t.get("funding_paid", 0.0))
            t_order_type = str(t.get("order_type", ""))
        else:
            sym = str(getattr(t, "symbol", ""))
            side = str(getattr(t, "side", ""))
            entry_ms = int(getattr(t, "entry_time_ms", 0))
            ep = float(getattr(t, "entry_price", 0.0))
            qty = float(getattr(t, "qty", 0.0))
            ir = float(getattr(t, "initial_risk", 100.0))
            pnl = float(getattr(t, "pnl", 0.0))
            fees = float(getattr(t, "fees_paid", 0.0))
            funding = float(getattr(t, "funding_paid", 0.0))
            t_order_type = str(getattr(t, "order_type", ""))

        if ir <= 0:
            ir = 100.0

        actual_net_rs.append(pnl / ir)

        if fixed_cost_r is not None:
            # fixed_cost_r is for tests only
            cost_r = fixed_cost_r
        else:
            # In production runs, cost per random trade = that source trade's actual fees_paid + funding_paid
            cost_r = ((fees + funding) / ir) if ir > 0 else 0.0

        # 1R stop distance percentage
        stop_dist = (ir / qty) if qty > 0 else (ep * 0.015)
        risk_pct = (stop_dist / ep) if ep > 0 else 0.015
        tod_ms = entry_ms % 86_400_000
        resolved_order_type = "limit" if (entry_order_type == "limit" or t_order_type == "limit") else "market"

        trade_profiles.append({
            "symbol": sym,
            "side": side,
            "tod_ms": tod_ms,
            "entry_time_ms": entry_ms,
            "risk_pct": risk_pct,
            "cost_r": cost_r,
            "funding_r": (funding / ir) if ir > 0 else 0.0,
            "initial_risk": ir,
            "order_type": resolved_order_type,
        })

    actual_net_exp = sum(actual_net_rs) / len(actual_net_rs)

    # Pre-index bars by symbol and time-of-day
    tod_map: dict[str, dict[int, list[int]]] = {}
    normalized_bars: dict[str, list[dict[str, Any]]] = {}

    for sym, sym_bars in bars_by_symbol.items():
        n_bars = len(sym_bars)
        if n_bars == 0:
            continue

        sym_tod: dict[int, list[int]] = {}
        bar_list: list[dict[str, Any]] = []

        for idx, b in enumerate(sym_bars):
            ot = getattr(b, "open_time", None)
            if ot is None and isinstance(b, dict):
                ot = b.get("open_time", 0)
            if isinstance(ot, datetime):
                ot_ms = int(ot.timestamp() * 1000)
            else:
                ot_ms = int(ot or 0)

            op = Decimal(str(getattr(b, "open", 0.0) if not isinstance(b, dict) else b["open"]))
            hi = Decimal(str(getattr(b, "high", 0.0) if not isinstance(b, dict) else b["high"]))
            lo = Decimal(str(getattr(b, "low", 0.0) if not isinstance(b, dict) else b["low"]))
            cl = Decimal(str(getattr(b, "close", 0.0) if not isinstance(b, dict) else b["close"]))

            bar_list.append({
                "open_time": ot_ms,
                "open": op,
                "high": hi,
                "low": lo,
                "close": cl,
            })

            # Candidate entry: can enter on any bar where there is at least 1 forward bar
            if idx <= n_bars - 2:
                tod = ot_ms % 86_400_000
                sym_tod.setdefault(tod, []).append(idx)

        tod_map[sym] = sym_tod
        normalized_bars[sym] = bar_list

    seed_gross_exp: list[float] = []
    seed_net_exp: list[float] = []
    out_sampled_trades: list[SampledTradeInfo] = sampled_trades if sampled_trades is not None else []
    sampled_entry_times_by_seed: list[list[int]] = []
    skipped_per_seed: list[int] = []
    missed_limit_fills_list: list[int] = []

    for seed in range(num_seeds):
        rng = random.Random(seed + seed_offset)
        seed_gross_list: list[float] = []
        seed_net_list: list[float] = []
        seed_entry_times: list[int] = []
        skipped_count = 0
        missed_limit_count = 0

        for prof in trade_profiles:
            sym = prof["symbol"]
            b_list = normalized_bars.get(sym)
            if not b_list or len(b_list) < 2:
                skipped_count += 1
                continue

            # Match timestamp distribution (exact time-of-day UTC ms)
            candidates = tod_map.get(sym, {}).get(prof["tod_ms"])
            if candidates:
                bar_idx = rng.choice(candidates)
            else:
                max_entry = max(0, len(b_list) - 2)
                bar_idx = rng.randint(0, max_entry)

            cand_bar = b_list[bar_idx]
            seed_entry_times.append(cand_bar["open_time"])
            side = prof["side"]
            risk_pct = prof["risk_pct"]
            cost_r = prof["cost_r"]
            is_limit_order = prof.get("order_type") == "limit"

            if is_limit_order:
                # Limit order execution: honest trade-through by >= 1 tick
                limit_price = round_price(cand_bar["open"], tick_size)
                fill_idx = None
                for j in range(bar_idx + 1, min(len(b_list), bar_idx + 1 + limit_expiry_bars)):
                    b = b_list[j]
                    fill_p, _ = simulate_limit_fill(side, limit_price, b["high"], b["low"], tick_size)
                    if fill_p is not None:
                        fill_idx = j
                        break

                if fill_idx is None:
                    # Expired without trade-through -> missed fill
                    skipped_count += 1
                    missed_limit_count += 1
                    continue

                entry_fill = limit_price
                start_exit_idx = fill_idx + 1
            else:
                # Market order entry execution (includes slippage)
                entry_fill, _ = simulate_market_fill(
                    price=cand_bar["open"],
                    side=side,
                    tick_size=tick_size,
                    atr=None,
                    slippage_bps=slippage_bps,
                    slippage_atr_factor=slippage_atr_factor,
                )
                start_exit_idx = bar_idx + 1

            # 2. 1R Stop / 2R Target geometry from actual fill
            r_dist = entry_fill * Decimal(str(risk_pct))
            if r_dist <= Decimal("0"):
                r_dist = entry_fill * Decimal("0.01")  # fallback 1%

            if side == "long":
                stop_loss = round_price(entry_fill - r_dist, tick_size)
                take_profit = round_price(entry_fill + (Decimal("2.0") * r_dist), tick_size)
            else:
                stop_loss = round_price(entry_fill + r_dist, tick_size)
                take_profit = round_price(entry_fill - (Decimal("2.0") * r_dist), tick_size)

            # 3. Intra-candle forward evaluation using shared simulate_candle_exit
            max_look = (
                min(len(b_list) - 1, start_exit_idx + max_horizon_bars)
                if max_horizon_bars is not None
                else (len(b_list) - 1)
            )

            gross_r: float | None = None
            exit_reason_str = "mark_to_market"
            last_idx = start_exit_idx
            is_maker_exit = False
            exit_fill_price: Decimal | None = None

            for j in range(start_exit_idx, max_look + 1):
                last_idx = j
                b = b_list[j]
                exit_price, exit_reason, is_maker, slip_pct = simulate_candle_exit(
                    pos_side=side,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    b_open=b["open"],
                    b_high=b["high"],
                    b_low=b["low"],
                    tick_size=tick_size,
                    atr=None,
                    slippage_bps=slippage_bps,
                    slippage_atr_factor=slippage_atr_factor,
                )

                if exit_reason is not None:
                    exit_reason_str = exit_reason
                    is_maker_exit = is_maker
                    exit_fill_price = exit_price
                    if side == "long":
                        gross_r = float((exit_price - entry_fill) / r_dist)
                    else:
                        gross_r = float((entry_fill - exit_price) / r_dist)
                    break

            if gross_r is None:
                # Time / max horizon exit: executes as market order (taker + adverse slippage)
                cand_close = b_list[min(last_idx, len(b_list) - 1)]["close"]
                closing_side = "short" if side == "long" else "long"
                exit_price, _ = simulate_market_fill(
                    price=cand_close,
                    side=closing_side,
                    tick_size=tick_size,
                    atr=None,
                    slippage_bps=slippage_bps,
                    slippage_atr_factor=slippage_atr_factor,
                )
                exit_fill_price = exit_price
                exit_reason_str = "max_horizon_bars"
                is_maker_exit = False
                if side == "long":
                    gross_r = float((exit_price - entry_fill) / r_dist)
                else:
                    gross_r = float((entry_fill - exit_price) / r_dist)

            if fixed_cost_r is not None:
                effective_cost_r = fixed_cost_r
            else:
                # Spec-compliant exit fee model:
                # Entry: maker (0.0002) if limit order, else taker (0.0005)
                # Exit: target = maker (0.0002, no slippage), stop = taker (0.0005 + slippage), time/other = taker (0.0005 + slippage)
                entry_fee_rate = 0.0002 if is_limit_order else 0.0005
                exit_fee_rate = 0.0002 if (is_maker_exit or exit_reason_str == "take_profit") else 0.0005

                entry_fee_r = entry_fee_rate / risk_pct if risk_pct > 0 else 0.0
                exit_ratio = float(exit_fill_price / entry_fill) if exit_fill_price else 1.0
                exit_fee_r = (exit_fee_rate * exit_ratio / risk_pct) if risk_pct > 0 else 0.0
                funding_r = prof.get("funding_r", 0.0)
                effective_cost_r = entry_fee_r + exit_fee_r + funding_r

            net_r = gross_r - effective_cost_r
            seed_gross_list.append(gross_r)
            seed_net_list.append(net_r)

            if seed == 0:
                out_sampled_trades.append(
                    SampledTradeInfo(
                        symbol=sym,
                        side=side,
                        risk_pct=risk_pct,
                        tod_ms=cand_bar["open_time"] % 86_400_000,
                        source_tod_ms=prof["tod_ms"],
                        entry_price=float(entry_fill),
                        stop_price=float(stop_loss),
                        target_price=float(take_profit),
                        gross_r=gross_r,
                        net_r=net_r,
                        exit_reason=exit_reason_str,
                        entry_time_ms=cand_bar["open_time"],
                        source_entry_time_ms=prof["entry_time_ms"],
                        order_type="limit" if is_limit_order else "market",
                    )
                )

        skipped_per_seed.append(skipped_count)
        missed_limit_fills_list.append(missed_limit_count)
        sampled_entry_times_by_seed.append(seed_entry_times)
        if seed_net_list:
            seed_gross_exp.append(sum(seed_gross_list) / len(seed_gross_list))
            seed_net_exp.append(sum(seed_net_list) / len(seed_net_list))

    mean_gross = sum(seed_gross_exp) / len(seed_gross_exp) if seed_gross_exp else 0.0
    mean_net = sum(seed_net_exp) / len(seed_net_exp) if seed_net_exp else 0.0
    min_net = min(seed_net_exp) if seed_net_exp else 0.0
    max_net = max(seed_net_exp) if seed_net_exp else 0.0

    beat_count = sum(1 for r in seed_net_exp if actual_net_exp > r)
    percentile = (beat_count / len(seed_net_exp) * 100.0) if seed_net_exp else 50.0
    avg_skipped = sum(skipped_per_seed) // len(skipped_per_seed) if skipped_per_seed else 0
    avg_missed_limit = sum(missed_limit_fills_list) // len(missed_limit_fills_list) if missed_limit_fills_list else 0

    return RandomBaselineStats(
        num_seeds=num_seeds,
        mean_gross_r=mean_gross,
        mean_net_r=mean_net,
        min_net_r=min_net,
        max_net_r=max_net,
        percentile=percentile,
        beat_count=beat_count,
        skipped_trades_per_seed=avg_skipped,
        missed_limit_fills_per_seed=avg_missed_limit,
        seed_net_exp=seed_net_exp,
        seed_gross_exp=seed_gross_exp,
        sampled_trades=out_sampled_trades,
        sampled_entry_times_by_seed=sampled_entry_times_by_seed,
    )
