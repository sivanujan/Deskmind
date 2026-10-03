"""H1 v2.0.0: Discount OB / FVG Continuation with honest limit order execution.

Pre-registered variants:
  - Variant A (H1_v2.0.0_A): Exact v1.0.0 signal conditions (same evaluate function),
    with entry changed from market to honest proximal limit order (N=12 expiry, trade-through fill).
  - Variant B (H1_v2.0.0_B): Variant A + 1h trend filter (htf_trend_1h in signal direction).
  - Variant C (H1_v2.0.0_C): Variant A + ICT Session Killzones (London Open & NY AM only).
"""

from __future__ import annotations

from typing import Any, Literal

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.models import Signal, make_setup_id


def compute_h1_proximal_limit_price(
    feature_row: FeatureRow | dict[str, Any], current_close: float, direction: str
) -> tuple[float, float, float]:
    """Compute proximal edge limit price, 1R stop, and 2R target from actual fill geometry."""
    r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

    fvg_size = float(r.get("nearest_fvg_size_atr", 0.0))
    fvg_dist = float(r.get("nearest_fvg_dist_atr", 999.0))
    ob_state = str(r.get("nearest_ob_state", "none"))
    ob_dist = float(r.get("nearest_ob_dist_atr", 999.0))

    candidates_dist = []
    if ob_dist <= 1.0 and ob_state == "fresh":
        candidates_dist.append(ob_dist)
    if fvg_dist <= 1.0 and fvg_size >= 0.5:
        candidates_dist.append(fvg_dist)
    dist_atr = min(candidates_dist) if candidates_dist else 0.0

    atr = current_close * float(r.get("atr_pct", 0.01))
    if atr <= 0:
        atr = current_close * 0.01

    if direction == "bullish":
        limit_entry = current_close - (dist_atr * atr)
        stop = limit_entry - (1.5 * atr)
        target = limit_entry + (3.0 * atr)
    else:
        limit_entry = current_close + (dist_atr * atr)
        stop = limit_entry + (1.5 * atr)
        target = limit_entry - (3.0 * atr)

    return limit_entry, stop, target


class H1DiscountOBPluginV2(HypothesisPlugin):
    """H1 v2.0.0 Discount OB / FVG strategy plugin with proximal limit entry."""

    def __init__(self, variant: Literal["A", "B", "C"] = "A") -> None:
        self.variant = variant
        self._v1_plugin = H1DiscountOBPlugin()

    @property
    def hypothesis_id(self) -> str:
        if self.variant == "A":
            return "H1_v2.0.0_A"
        elif self.variant == "B":
            return "H1_v2.0.0_B"
        elif self.variant == "C":
            return "H1_v2.0.0_C"
        return "H1_v2.0.0"

    @property
    def name(self) -> str:
        if self.variant == "A":
            return "Discount OB/FVG - Proximal Limit (Variant A)"
        elif self.variant == "B":
            return "Discount OB/FVG - Limit + 1h Trend (Variant B)"
        elif self.variant == "C":
            return "Discount OB/FVG - Limit + Killzones (Variant C)"
        return "Discount OB / FVG Continuation v2.0.0"

    def evaluate(
        self, feature_row: FeatureRow | dict[str, Any], current_close: float
    ) -> Signal | None:
        # Variant A evaluates the EXACT same signal condition function as v1.0.0
        v1_sig = self._v1_plugin.evaluate(feature_row, current_close)
        if v1_sig is None:
            return None

        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        # Variant B: 1h Trend Direction Filter (must match frozen spec)
        if self.variant == "B":
            t_1h = str(r.get("htf_trend_1h", "ranging"))
            if t_1h != v1_sig.direction:
                return None

        # Variant C: London & NY AM killzone filter
        if self.variant == "C":
            kz = str(r.get("killzone", "none"))
            if kz not in ("london_open", "ny_am"):
                return None

        # Convert entry from market to proximal limit order, preserving exact stop/target geometry
        limit_entry, stop, target = compute_h1_proximal_limit_price(
            feature_row, current_close, v1_sig.direction
        )

        return Signal(
            hypothesis_id=self.hypothesis_id,
            symbol=v1_sig.symbol,
            timestamp=v1_sig.timestamp,
            direction=v1_sig.direction,
            entry_price=limit_entry,
            stop_price=stop,
            target_price=target,
            r_multiple=2.0,
            rationale=f"H1 v2 ({self.variant}): v1 setup + honest proximal limit ({v1_sig.direction})",
            setup_id=v1_sig.setup_id,
            order_type="limit",
            limit_price=limit_entry,
            limit_expiry_bars=12,
        )


class H1V2VariantAPlugin(H1DiscountOBPluginV2):
    def __init__(self) -> None:
        super().__init__(variant="A")


class H1V2VariantBPlugin(H1DiscountOBPluginV2):
    def __init__(self) -> None:
        super().__init__(variant="B")


class H1V2VariantCPlugin(H1DiscountOBPluginV2):
    def __init__(self) -> None:
        super().__init__(variant="C")
