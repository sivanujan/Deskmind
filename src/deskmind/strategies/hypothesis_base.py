"""Base strategy adapter for research hypothesis plugins.

Bridges research hypotheses (H1 to H5) to the real BacktestEngine execution pipeline.
Evaluates point-in-time features or agent state at each bar close, emitting exactly
one Intent per unique setup_id to ensure setup-level deduplication consistency between
the research funnel and the backtest engine.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from deskmind.features.store import FeatureStore
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h1_v2 import (
    H1V2VariantAPlugin,
    H1V2VariantBPlugin,
    H1V2VariantCPlugin,
)
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin
from deskmind.strategies.base import BaseStrategy, Intent, StrategyContext


class BaseHypothesisStrategy(BaseStrategy):
    uses_agents = ("trend", "ict_smc", "ict_time", "price_action")
    timeframes = ("1h", "15m", "5m")

    def __init__(
        self,
        plugin: HypothesisPlugin,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(params)
        self.plugin = plugin
        self._emitted_setup_ids: set[str] = set()
        self._loaded_symbols: set[str] = set()
        self._feature_lookup: dict[str, dict[str, Any]] = {}
        initial_lookup = self.params.get("feature_lookup") or {}
        for k, v in initial_lookup.items():
            self._index_row(v)
            sym = v.get("symbol")
            if sym:
                self._loaded_symbols.add(sym)
        self._feature_store = FeatureStore(
            base_dir=self.params.get("feature_dir", "data/features")
        )

    def _bar_open_key(self, symbol: str, ts_ms: int) -> str:
        open_ms = ((int(ts_ms) - 1) // 300_000) * 300_000
        return f"{symbol}:{open_ms}"

    def _index_row(self, row: dict[str, Any]) -> None:
        sym = row.get("symbol")
        ts = row.get("timestamp")
        if sym and ts is not None:
            ts_int = int(ts)
            self._feature_lookup[f"{sym}:{ts_int}"] = row
            self._feature_lookup[f"{sym}:{ts_int + 1}"] = row
            self._feature_lookup[f"{sym}:{ts_int - 1}"] = row
            self._feature_lookup[self._bar_open_key(sym, ts_int)] = row

    def _ensure_features_loaded(self, symbol: str) -> None:
        if symbol in self._loaded_symbols:
            return
        rows = self._feature_store.read_features(symbol=symbol, split=None)
        for r in rows:
            self._index_row(r)
        self._loaded_symbols.add(symbol)

    def on_bar(self, contexts: dict[str, StrategyContext]) -> list[Intent]:
        intents: list[Intent] = []

        for symbol, ctx in contexts.items():
            self._ensure_features_loaded(symbol)
            row = (
                self._feature_lookup.get(f"{symbol}:{ctx.as_of_ms}")
                or self._feature_lookup.get(f"{symbol}:{ctx.as_of_ms - 1}")
                or self._feature_lookup.get(self._bar_open_key(symbol, ctx.as_of_ms))
            )
            if not row:
                continue

            close_f = float(ctx.current_bar.close)
            sig = self.plugin.evaluate(row, close_f)
            if sig is None:
                continue

            # Shared setup_id consistency: each hypothesis emits exactly one signal per setup_id.
            # Repeated bars of the same setup are ignored so engine raw signals == funnel unique setups.
            sid = sig.setup_id or f"{sig.symbol}:{sig.timestamp}:{sig.direction}"
            if sid in self._emitted_setup_ids:
                continue
            self._emitted_setup_ids.add(sid)

            action = "open_long" if sig.direction == "bullish" else "open_short"
            stop_val = Decimal(str(sig.stop_price))
            target_val = (
                Decimal(str(sig.target_price)) if sig.target_price else None
            )

            if stop_val <= Decimal("0"):
                continue

            order_type = getattr(sig, "order_type", "market") or "market"
            limit_val = (
                Decimal(str(sig.limit_price))
                if getattr(sig, "limit_price", None) is not None
                else None
            )

            intents.append(
                Intent(
                    symbol=symbol,
                    action=action,
                    order_type=order_type,
                    limit_price=limit_val,
                    stop_loss=stop_val,
                    take_profit=target_val,
                    ranking_score=1.0,
                    setup_id=sid,
                    metadata={
                        "hypothesis_id": sig.hypothesis_id,
                        "rationale": sig.rationale,
                        "setup_id": sid,
                        "r_multiple": sig.r_multiple,
                    },
                )
            )

        return intents


class H1DiscountOBStrategy(BaseHypothesisStrategy):
    name = "h1_discount_ob"
    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H1DiscountOBPlugin(), params)


class H1V2VariantAStrategy(BaseHypothesisStrategy):
    name = "h1_v2_variant_a"
    version = "2.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H1V2VariantAPlugin(), params)


class H1V2VariantBStrategy(BaseHypothesisStrategy):
    name = "h1_v2_variant_b"
    version = "2.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H1V2VariantBPlugin(), params)


class H1V2VariantCStrategy(BaseHypothesisStrategy):
    name = "h1_v2_variant_c"
    version = "2.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H1V2VariantCPlugin(), params)


class H2SweepReversalStrategy(BaseHypothesisStrategy):
    name = "h2_sweep_reversal"
    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H2SweepReversalPlugin(), params)


class H3SilverBulletStrategy(BaseHypothesisStrategy):
    name = "h3_silver_bullet"
    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H3SilverBulletPlugin(), params)


class H4BreakoutRetestStrategy(BaseHypothesisStrategy):
    name = "h4_breakout_retest"
    version = "1.1.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H4BreakoutRetestPlugin(), params)


class H5PDSweepStrategy(BaseHypothesisStrategy):
    name = "h5_pd_sweep"
    version = "1.0.0"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(H5PDSweepPlugin(), params)


from deskmind.strategies.h6_trend_breakout import H6TrendBreakoutStrategy
