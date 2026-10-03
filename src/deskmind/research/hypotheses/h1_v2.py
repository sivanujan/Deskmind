"""H1 v2.0.0: Discount OB / FVG Continuation with honest limit order execution.

Three pre-registered variants:
  - Variant A (H1_v2.0.0_A): Proximal limit entry at OB/FVG edge (N=12 expiry, trade-through fill).
  - Variant B (H1_v2.0.0_B): Variant A + 1h trend filter (trade strictly in 1h trend direction).
  - Variant C (H1_v2.0.0_C): Variant A + ICT Session Killzones (London Open & NY AM only).
"""

from __future__ import annotations

from typing import Any, Literal

from deskmind.features.models import FeatureRow
from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.models import Signal, make_setup_id


class H1DiscountOBPluginV2(HypothesisPlugin):
    """H1 v2.0.0 Discount OB / FVG strategy plugin with proximal limit entry."""

    def __init__(self, variant: Literal["A", "B", "C"] = "A") -> None:
        self.variant = variant

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
        r = feature_row.as_dict() if isinstance(feature_row, FeatureRow) else feature_row

        t_1h = str(r.get("htf_trend_1h", "ranging"))
        pd_zone = str(r.get("pd_zone_1h", "none"))
        fvg_size = float(r.get("nearest_fvg_size_atr", 0.0))
        fvg_dist = float(r.get("nearest_fvg_dist_atr", 999.0))
        ob_state = str(r.get("nearest_ob_state", "none"))
        ob_dist = float(r.get("nearest_ob_dist_atr", 999.0))
        ev_5m = str(r.get("last_event_5m", "none"))
        kz = str(r.get("killzone", "none"))

        # Variant C: London & NY AM killzone filter
        if self.variant == "C" and kz not in ("london_open", "ny_am"):
            return None

        # Check POI qualification: fresh 15m OB or FVG >= 0.5 ATR within 1.0 ATR
        fvg_ok = (fvg_dist <= 1.0 and fvg_size >= 0.5)
        ob_ok = (ob_dist <= 1.0 and ob_state == "fresh")
        if not (fvg_ok or ob_ok):
            return None

        # Distance to proximal edge in ATR units
        candidates_dist = []
        if ob_ok:
            candidates_dist.append(ob_dist)
        if fvg_ok:
            candidates_dist.append(fvg_dist)
        dist_atr = min(candidates_dist) if candidates_dist else 0.0

        atr = current_close * float(r.get("atr_pct", 0.01))
        if atr <= 0:
            atr = current_close * 0.01

        sym = str(r.get("symbol", "UNKNOWN"))
        ts = int(r.get("timestamp", 0))
        ev_confirmed = int(r.get("last_event_5m_confirmed_at") or ts)

        # Bullish setup: 1h discount + 5m CHoCH_up
        if pd_zone == "discount" and ev_5m == "CHoCH_up":
            # Variant B: 1h trend must be bullish
            if self.variant == "B" and t_1h != "bullish":
                return None

            limit_entry = current_close - (dist_atr * atr)
            stop = limit_entry - (1.5 * atr)
            target = limit_entry + (3.0 * atr)  # 2.0R target from fill
            setup_id = make_setup_id(f"H1_v2_{self.variant}", sym, ev_confirmed, "bullish")

            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bullish",
                entry_price=limit_entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H1 v2 ({self.variant}) Bullish: discount + proximal limit + 5m CHoCH_up",
                setup_id=setup_id,
                order_type="limit",
                limit_price=limit_entry,
                limit_expiry_bars=12,
            )

        # Bearish setup: 1h premium + 5m CHoCH_down
        if pd_zone == "premium" and ev_5m == "CHoCH_down":
            # Variant B: 1h trend must be bearish
            if self.variant == "B" and t_1h != "bearish":
                return None

            limit_entry = current_close + (dist_atr * atr)
            stop = limit_entry + (1.5 * atr)
            target = limit_entry - (3.0 * atr)  # 2.0R target from fill
            setup_id = make_setup_id(f"H1_v2_{self.variant}", sym, ev_confirmed, "bearish")

            return Signal(
                hypothesis_id=self.hypothesis_id,
                symbol=sym,
                timestamp=ts,
                direction="bearish",
                entry_price=limit_entry,
                stop_price=stop,
                target_price=target,
                r_multiple=2.0,
                rationale=f"H1 v2 ({self.variant}) Bearish: premium + proximal limit + 5m CHoCH_down",
                setup_id=setup_id,
                order_type="limit",
                limit_price=limit_entry,
                limit_expiry_bars=12,
            )

        return None


class H1V2VariantAPlugin(H1DiscountOBPluginV2):
    def __init__(self) -> None:
        super().__init__(variant="A")


class H1V2VariantBPlugin(H1DiscountOBPluginV2):
    def __init__(self) -> None:
        super().__init__(variant="B")


class H1V2VariantCPlugin(H1DiscountOBPluginV2):
    def __init__(self) -> None:
        super().__init__(variant="C")
