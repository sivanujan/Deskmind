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
from deskmind.research.hypotheses.h6_trend_breakout import H6TrendBreakoutPlugin


VOID_HYPOTHESES: dict[str, str] = {
    "H4_v1.0.0": "VOID (Spec/Scale Mismatch): nearest_sr_strength >= 2 was trivially true (99.3% of zones score 40-59). Counted as Trial #1; superseded by pre-registered H4_v1.1.0 (nearest_sr_touches >= 3).",
}

# Frozen specification file and SHA-256 hash for H1 v2.0.0
H1_V2_SPEC_PATH: str = "docs/research/hypotheses/H1_v2.md"
H1_V2_FROZEN_SHA256: str = "50a4f2558de47a700a9196f576c61e631f757b626852d79365ca1993f80db4a4"

# Frozen specification file and SHA-256 hash for H6 v1.0.0
H6_SPEC_PATH: str = "docs/research/hypotheses/H6.md"
H6_FROZEN_SHA256: str = "3d2a11046a4e58f59ec51970b1707d242845cb88b82fd7925b71a34e245335c3"


def verify_h1_v2_spec_hash() -> bool:
    """Verify that docs/research/hypotheses/H1_v2.md is byte-identical to the frozen pre-registered spec."""
    import hashlib
    from pathlib import Path

    p = Path(H1_V2_SPEC_PATH)
    if not p.exists():
        return False
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return h.lower() == H1_V2_FROZEN_SHA256.lower()


def verify_h6_spec_hash() -> bool:
    """Verify that docs/research/hypotheses/H6.md is byte-identical to the frozen pre-registered spec."""
    import hashlib
    from pathlib import Path

    p = Path(H6_SPEC_PATH)
    if not p.exists():
        return False
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return h.lower() == H6_FROZEN_SHA256.lower()


class HypothesisRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, HypothesisPlugin] = {
            "H1_v1.0.0": H1DiscountOBPlugin(),
            "H2_v1.0.0": H2SweepReversalPlugin(),
            "H3_v1.0.0": H3SilverBulletPlugin(),
            "H4_v1.1.0": H4BreakoutRetestPlugin(),
            "H5_v1.0.0": H5PDSweepPlugin(),
            "H6_v1.0.0": H6TrendBreakoutPlugin(),
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
            "H6_v1.0.0": 1,
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

    def get_program_trials(self) -> list[str]:
        """Return distinct program-wide trial IDs across all evaluated and pre-registered hypotheses.

        Trials: H1 v1, H2, H3, H4 v1.0.0 (void), H4 v1.1.0, H5, + 3 H1 v2 variants = K=9.
        """
        trials = set(self._plugins.keys()) | set(VOID_HYPOTHESES.keys())
        trials.update(k for k in self._v2_plugins.keys() if k != "H1_v2.0.0")
        return sorted(trials)

    def get_program_trial_count(self) -> int:
        """Compute K program-wide trial count automatically from the registry.

        Returns K = 9.
        """
        return len(self.get_program_trials())

    def get_trial_count(self, hypothesis_id: str | None = None) -> int:
        """Return the program-wide trial count (K=9) for Deflated Sharpe Ratio calculation."""
        return self.get_program_trial_count()


REGISTRY = HypothesisRegistry()
