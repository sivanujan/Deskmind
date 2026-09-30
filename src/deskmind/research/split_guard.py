"""Train-only split guard for research framework and feature store readers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Sequence

from deskmind.config import BACKTEST_SPLITS
from deskmind.research.gates import GateCheckResult


def parse_iso_ms(iso_str: str, end_of_day: bool = False) -> int:
    """Parse 'YYYY-MM-DD' into UTC millisecond timestamp."""
    dt = datetime.strptime(iso_str, "%Y-%m-%d").replace(tzinfo=UTC)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(dt.timestamp() * 1000)


SPLIT_BOUNDS_MS: dict[str, tuple[int, int]] = {
    name: (parse_iso_ms(start), parse_iso_ms(end, end_of_day=True))
    for name, (start, end) in BACKTEST_SPLITS.items()
}


class SplitGuard:
    @staticmethod
    def get_bounds(split: str = "train") -> tuple[int, int]:
        """Get (start_ms, end_ms) for a named split."""
        split_lower = split.lower()
        if split_lower not in SPLIT_BOUNDS_MS:
            raise ValueError(f"Unknown split: '{split}'. Allowed splits: {list(SPLIT_BOUNDS_MS)}")
        return SPLIT_BOUNDS_MS[split_lower]

    @staticmethod
    def validate_split_request(
        split: str = "train",
        hypothesis_id: str | None = None,
        gate_result: GateCheckResult | None = None,
    ) -> None:
        """Enforce strict access rules across research splits:

        - 'train': Always allowed (default).
        - 'validation': Refused UNLESS gate_result exists and passed=True.
        - 'test': Permanently locked.
        """
        split_lower = split.lower()
        if split_lower == "test":
            raise PermissionError("The 'test' split is permanently locked for research and backtesting.")

        if split_lower == "validation":
            if gate_result is None or not gate_result.passed:
                raise PermissionError(
                    f"Running hypothesis '{hypothesis_id or 'unknown'}' on 'validation' split is refused. "
                    "Hypothesis must pass all TRAIN research gates first."
                )

    @staticmethod
    def filter_features_by_split(
        rows: Sequence[dict[str, Any]], split: str = "train"
    ) -> list[dict[str, Any]]:
        """Filter feature rows strictly within requested split date boundaries."""
        start_ms, end_ms = SplitGuard.get_bounds(split)
        return [r for r in rows if start_ms <= r.get("timestamp", 0) <= end_ms]
