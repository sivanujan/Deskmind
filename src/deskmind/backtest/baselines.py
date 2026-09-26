"""Baseline simulations for honest backtest evaluation.

1. Buy & Hold baseline per symbol (accounting for fees and real historical funding).
2. Random-entry baseline: same number of trades, same direction mix, same stop/target/holding logic,
   random entry times across multiple random seeds.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from deskmind.backtest.engine import BacktestResult, load_funding_map
from deskmind.config import DEFAULT_TAKER_FEE_RATE
from deskmind.data.storage import load_klines


@dataclass
class BuyAndHoldResult:
    symbol: str
    start_price: float
    end_price: float
    price_return_pct: float
    net_return_pct: float
    fees_paid_usd: float
    funding_paid_usd: float


@dataclass
class RandomBaselineResult:
    num_seeds: int
    our_net_return_usd: float
    percentile: float
    beat_count: int
    median_random_return_usd: float
    mean_random_return_usd: float
    min_random_return_usd: float
    max_random_return_usd: float
    verdict: str  # "beats_random" or "does_not_beat_random"
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    confidence_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "num_seeds": self.num_seeds,
            "our_net_return_usd": self.our_net_return_usd,
            "percentile": self.percentile,
            "beat_count": self.beat_count,
            "median_random_return_usd": self.median_random_return_usd,
            "mean_random_return_usd": self.mean_random_return_usd,
            "min_random_return_usd": self.min_random_return_usd,
            "max_random_return_usd": self.max_random_return_usd,
            "verdict": self.verdict,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "confidence_note": self.confidence_note,
        }


def calculate_single_buy_and_hold(
    symbol: str,
    start_price: Decimal,
    end_price: Decimal,
    alloc_cash: Decimal,
    funding_rates: list[Decimal],
    taker_fee_rate: Decimal = DEFAULT_TAKER_FEE_RATE,
) -> BuyAndHoldResult:
    """Compute buy & hold result for a single asset with fees and funding."""
    qty = alloc_cash / start_price
    entry_fee = alloc_cash * taker_fee_rate
    exit_fee = (qty * end_price) * taker_fee_rate
    total_fees = entry_fee + exit_fee

    total_funding = Decimal("0")
    for r in funding_rates:
        total_funding += alloc_cash * r

    gross_pnl = (end_price - start_price) * qty
    net_pnl = gross_pnl - total_fees - total_funding
    net_return_pct = (net_pnl / alloc_cash) * Decimal("100")
    price_return_pct = ((end_price - start_price) / start_price) * Decimal("100")

    return BuyAndHoldResult(
        symbol=symbol,
        start_price=float(start_price),
        end_price=float(end_price),
        price_return_pct=float(price_return_pct),
        net_return_pct=float(net_return_pct),
        fees_paid_usd=float(total_fees),
        funding_paid_usd=float(total_funding),
    )


def compute_buy_and_hold(
    data_dir: Path,
    symbols: list[str],
    start: datetime,
    end: datetime,
    initial_cash: Decimal,
) -> dict[str, BuyAndHoldResult]:
    """Simulate Buy & Hold holding each symbol with equal initial allocation."""
    results: dict[str, BuyAndHoldResult] = {}
    alloc_per_sym = initial_cash / Decimal(str(len(symbols))) if symbols else initial_cash

    for sym in symbols:
        tbl = load_klines(data_dir, sym, "5m", start, end)
        if tbl.num_rows < 2:
            continue

        pydict = tbl.to_pydict()
        start_price = Decimal(str(pydict["open"][0]))
        end_price = Decimal(str(pydict["close"][-1]))

        # Historical funding
        funding_map = load_funding_map(data_dir, sym)
        start_ms = int(pydict["open_time"][0].timestamp() * 1000)
        end_ms = int(pydict["close_time"][-1].timestamp() * 1000)

        f_rates = [f_rate for f_time, f_rate in funding_map.items() if start_ms <= f_time <= end_ms]

        results[sym] = calculate_single_buy_and_hold(
            symbol=sym,
            start_price=start_price,
            end_price=end_price,
            alloc_cash=alloc_per_sym,
            funding_rates=f_rates,
            taker_fee_rate=DEFAULT_TAKER_FEE_RATE,
        )

    return results


def run_random_baseline(
    result: BacktestResult,
    data_dir: Path,
    symbols: list[str],
    start: datetime,
    end: datetime,
    num_seeds: int = 1000,
) -> RandomBaselineResult:
    """Run random-entry baseline simulation with matching trade count and direction mix."""
    trades = result.trades
    total_trades = len(trades)
    our_return = float(result.final_equity - result.initial_cash)

    if total_trades == 0:
        return RandomBaselineResult(
            num_seeds=num_seeds,
            our_net_return_usd=our_return,
            percentile=50.0,
            beat_count=0,
            median_random_return_usd=0.0,
            mean_random_return_usd=0.0,
            min_random_return_usd=0.0,
            max_random_return_usd=0.0,
            verdict="does_not_beat_random",
        )

    # Direction mix
    sides = [t.side for t in trades]
    symbols_seq = [t.symbol for t in trades]
    avg_risk_usd = float(sum((t.initial_risk for t in trades), Decimal("0")) / total_trades)

    # Load 5m candles for trade evaluation
    bars_by_sym: dict[str, list[dict[str, float]]] = {}
    for s in symbols:
        tbl = load_klines(data_dir, s, "5m", start, end)
        if tbl.num_rows > 0:
            pd = tbl.to_pydict()
            bars_by_sym[s] = [
                {
                    "open": float(pd["open"][i]),
                    "high": float(pd["high"][i]),
                    "low": float(pd["low"][i]),
                    "close": float(pd["close"][i]),
                }
                for i in range(tbl.num_rows)
            ]

    random_returns: list[float] = []

    for seed in range(num_seeds):
        rng = random.Random(seed + 42)
        seed_pnl = 0.0

        for i in range(total_trades):
            sym = symbols_seq[i]
            side = sides[i]
            bars = bars_by_sym.get(sym, [])
            if len(bars) < 200:
                continue

            # Random entry index (leaving room for trade to play out)
            entry_idx = rng.randint(0, len(bars) - 100)
            entry_price = bars[entry_idx]["open"]
            if entry_price <= 0:
                continue

            # Estimate 1R stop distance as ~1% or avg ATR
            risk_dist = entry_price * 0.015  # 1.5% stop
            qty = avg_risk_usd / risk_dist if risk_dist > 0 else 0.0

            if side == "long":
                stop_loss = entry_price - risk_dist
                take_profit = entry_price + (2 * risk_dist)
            else:
                stop_loss = entry_price + risk_dist
                take_profit = entry_price - (2 * risk_dist)

            # Evaluate forward up to max 100 bars (~8 hours)
            trade_pnl = 0.0
            for k in range(entry_idx + 1, min(len(bars), entry_idx + 101)):
                b = bars[k]
                if side == "long":
                    if b["low"] <= stop_loss and b["high"] >= take_profit:
                        trade_pnl = -avg_risk_usd  # conservative SL first
                        break
                    elif b["low"] <= stop_loss:
                        trade_pnl = -avg_risk_usd
                        break
                    elif b["high"] >= take_profit:
                        trade_pnl = 2 * avg_risk_usd
                        break
                else:
                    if (b["high"] >= stop_loss and b["low"] <= take_profit) or b[
                        "high"
                    ] >= stop_loss:
                        trade_pnl = -avg_risk_usd
                        break
                    elif b["low"] <= take_profit:
                        trade_pnl = 2 * avg_risk_usd
                        break
            else:
                # Time exit at close of last bar
                last_bar = bars[min(len(bars) - 1, entry_idx + 100)]
                if side == "long":
                    trade_pnl = (last_bar["close"] - entry_price) * qty
                else:
                    trade_pnl = (entry_price - last_bar["close"]) * qty

            # Deduct round-trip taker fees (~0.1%)
            notional = entry_price * qty
            trade_pnl -= notional * 0.001
            seed_pnl += trade_pnl

        random_returns.append(seed_pnl)

    random_returns.sort()
    beat_count = sum(1 for r in random_returns if our_return >= r)
    percentile = (beat_count / len(random_returns)) * 100.0 if random_returns else 50.0

    # 95% Wilson score interval for the percentile
    p = beat_count / len(random_returns) if random_returns else 0.5
    n = len(random_returns) if random_returns else 1
    z = 1.95996  # 95% confidence
    denom = 1 + (z**2) / n
    centre = (p + (z**2) / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) / n) + ((z**2) / (4 * (n**2))))) / denom
    ci_lower = max(0.0, (centre - margin) * 100.0)
    ci_upper = min(100.0, (centre + margin) * 100.0)
    conf_note = (
        f"With N={num_seeds} random seeds, the 95% Wilson confidence interval for the true "
        f"percentile is [{ci_lower:.1f}%, {ci_upper:.1f}%]."
    )

    median_val = random_returns[len(random_returns) // 2] if random_returns else 0.0
    mean_val = (sum(random_returns) / len(random_returns)) if random_returns else 0.0
    min_val = min(random_returns) if random_returns else 0.0
    max_val = max(random_returns) if random_returns else 0.0

    verdict = "beats_random" if percentile >= 50.0 else "does_not_beat_random"

    return RandomBaselineResult(
        num_seeds=num_seeds,
        our_net_return_usd=our_return,
        percentile=percentile,
        beat_count=beat_count,
        median_random_return_usd=median_val,
        mean_random_return_usd=mean_val,
        min_random_return_usd=min_val,
        max_random_return_usd=max_val,
        verdict=verdict,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        confidence_note=conf_note,
    )
