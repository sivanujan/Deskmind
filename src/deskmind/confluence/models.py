"""Models for MTF alignment and deterministic equal-weight confluence score."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ConfluenceResult:
    alignment: str  # "aligned" | "partial" | "conflict"
    bias: str       # "bullish" | "bearish" | "neutral"
    score: float    # Sum of equal-weight components (0.0 to 10.0)
    max_score: float = 10.0
    components: dict[str, float] = None  # Component name -> score (0.0 or 1.0)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.components is None:
            d["components"] = {}
        return d
