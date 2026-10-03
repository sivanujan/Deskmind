"""Registry of pre-registered research hypotheses and version/trial tracker."""

from __future__ import annotations

from typing import Sequence

from deskmind.research.hypotheses.base import HypothesisPlugin
from deskmind.research.hypotheses.h1_discount_ob import H1DiscountOBPlugin
from deskmind.research.hypotheses.h1_v2 import (
    H1DiscountOBPluginV2,
    H1V2VariantAPlugin,
    H1V2VariantBPlugin,
    H1V2VariantCPlugin,
)
from deskmind.research.hypotheses.h2_sweep_reversal import H2SweepReversalPlugin
from deskmind.research.hypotheses.h3_silver_bullet import H3SilverBulletPlugin
from deskmind.research.hypotheses.h4_breakout_retest import H4BreakoutRetestPlugin
from deskmind.research.hypotheses.h5_pd_sweep import H5PDSweepPlugin


VOID_HYPOTHESES: dict[str, str] = {
    "H4_v1.0.0": "VOID (Spec/Scale Mismatch): nearest_sr_strength >= 2 was trivially true (99.3% of zones score 40-59). Counted as Trial #1; superseded by pre-registered H4_v1.1.0 (nearest_sr_touches >= 3).",
}

# Frozen specification file and SHA-256 hash for H1 v2.0.0
H1_V2_SPEC_PATH: str = "docs/research/hypotheses/H1_v2.md"
H1_V2_FROZEN_SHA256: str = "e2da92e2e729d09a8d82d6df59ce48dc02061392cc20cd5dbaab040c717d3390"


def verify_h1_v2_spec_hash() -> bool:
    """Verify that docs/research/hypotheses/H1_v2.md is byte-identical to the frozen pre-registered spec."""
    import hashlib
    from pathlib import Path

    p = Path(H1_V2_SPEC_PATH)
    if not p.exists():
        return False
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return h.lower() == H1_V2_FROZEN_SHA256.lower()


class HypothesisRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, HypothesisPlugin] = {
            "H1_v1.0.0": H1DiscountOBPlugin(),
            "H2_v1.0.0": H2SweepReversalPlugin(),
            "H3_v1.0.0": H3SilverBulletPlugin(),
            "H4_v1.1.0": H4BreakoutRetestPlugin(),
            "H5_v1.0.0": H5PDSweepPlugin(),
        }
        self._v2_plugins: dict[str, HypothesisPlugin] = {
            "H1_v2.0.0": H1DiscountOBPluginV2(variant="A"),
            "H1_v2.0.0_A": H1V2VariantAPlugin(),
            "H1_v2.0.0_B": H1V2VariantBPlugin(),
            "H1_v2.0.0_C": H1V2VariantCPlugin(),
        }
        self._trial_counts: dict[str, int] = {
            "H1_v1.0.0": 1,
            "H1_v2.0.0": 4,     # Trial count = 4 (v1 + 3 variants A, B, C)
            "H1_v2.0.0_A": 4,   # Variant A
            "H1_v2.0.0_B": 4,   # Variant B
            "H1_v2.0.0_C": 4,   # Variant C
            "H2_v1.0.0": 1,
            "H3_v1.0.0": 1,
            "H4_v1.0.0": 1,     # Trial #1 (VOID)
            "H4_v1.1.0": 1,     # Trial #2 (ACTIVE / PRE-REGISTERED)
            "H5_v1.0.0": 1,
        }

    def list_hypotheses(self, include_v2: bool = False) -> Sequence[HypothesisPlugin]:
        if include_v2:
            return list(self._plugins.values()) + list(self._v2_plugins.values())
        return list(self._plugins.values())

    def get_plugin(self, hypothesis_id: str) -> HypothesisPlugin | None:
        if hypothesis_id in self._plugins:
            return self._plugins[hypothesis_id]
        return self._v2_plugins.get(hypothesis_id)

    def record_trial(self, hypothesis_id: str) -> int:
        """Increment and return trial count for a given hypothesis version."""
        if hypothesis_id in self._trial_counts:
            self._trial_counts[hypothesis_id] += 1
            return self._trial_counts[hypothesis_id]
        self._trial_counts[hypothesis_id] = 1
        return 1

    def get_trial_count(self, hypothesis_id: str) -> int:
        if hypothesis_id.startswith("H1_v2"):
            return 4
        return self._trial_counts.get(hypothesis_id, 1)


REGISTRY = HypothesisRegistry()
