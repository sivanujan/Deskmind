"""H1 Discount OB Strategy adapter for the BacktestEngine.

Bridges research hypothesis H1 to the real BacktestEngine execution pipeline.
Evaluates point-in-time features or agent state at each bar close, emitting Intents
with setup_ids so the engine enforces max 1 open position, setup deduplication,
Risk Manager sizing, circuit breakers, and fee/slippage/funding modeling.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from deskmind.features.store import FeatureStore
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.strategies.base import BaseStrategy, Intent, StrategyContext


class H1DiscountOBStrategy(BaseStrategy):
    name = "h1_discount_ob"
    version = "1.0.0"
    default_params = {
        "r_multiple": 2.0,
    }
    uses_agents = ("trend", "ict_smc")
    timeframes = ("1h", "15m", "5m")

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        super().__init__(params)
        self.plugin = H1DiscountOBPlugin()
        self._feature_lookup: dict[str, dict[str, Any]] = {}
        initial_lookup = self.params.get("feature_lookup") or {}
        for k, v in initial_lookup.items():
            self._index_row(v)
        self._feature_store = FeatureStore(base_dir=self.params.get("feature_dir", "data/features"))
        self._loaded_symbols: set[str] = set()

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
        if symbol in self._loaded_symbols or self._feature_lookup:
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

            action = "open_long" if sig.direction == "bullish" else "open_short"
            stop_val = Decimal(str(round(sig.stop_price, 2)))
            target_val = Decimal(str(round(sig.target_price, 2))) if sig.target_price else None

            if stop_val <= Decimal("0"):
                continue

            intents.append(
                Intent(
                    symbol=symbol,
                    action=action,
                    order_type="market",
                    stop_loss=stop_val,
                    take_profit=target_val,
                    ranking_score=1.0,
                    setup_id=sig.setup_id,
                    metadata={
                        "hypothesis_id": sig.hypothesis_id,
                        "rationale": sig.rationale,
                        "setup_id": sig.setup_id,
                        "r_multiple": sig.r_multiple,
                    },
                )
            )

        return intents
