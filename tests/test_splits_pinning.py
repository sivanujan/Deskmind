"""Tests pinning research & backtest data splits and locking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmind.config import BACKTEST_SPLITS
from deskmind.research.split_guard import SplitGuard


def test_splits_pinned_exact_dates() -> None:
    """Pins exact required splits:

    TRAIN: 2023-01-01 -> 2024-12-31
    VALIDATION: 2025-01-01 -> 2025-12-31
    TEST: 2026-01-01 -> latest (locked)
    """
    assert BACKTEST_SPLITS["train"] == ("2023-01-01", "2024-12-31")
    assert BACKTEST_SPLITS["validation"] == ("2025-01-01", "2025-12-31")
    assert BACKTEST_SPLITS["test"] == ("2026-01-01", "latest")


def test_test_split_permanently_locked_in_research() -> None:
    with pytest.raises(PermissionError, match="locked"):
        SplitGuard.validate_split_request(split="test")


def test_validation_split_requires_passed_gates() -> None:
    with pytest.raises(PermissionError, match="refused"):
        SplitGuard.validate_split_request(split="validation", gate_result=None)


def test_no_run_in_registry_accessed_test_split() -> None:
    registry_file = Path("reports/backtests/registry.json")
    if not registry_file.exists():
        return
    with open(registry_file, encoding="utf-8") as f:
        runs = json.load(f)

    for r in runs:
        split = r.get("split", "").lower()
        start = r.get("start_date", "")
        end = r.get("end_date", "")
        assert split != "test", f"Found test-split run in registry: {r.get('run_id')}"
        assert start < "2026-01-01", f"Found run starting in test split: {r.get('run_id')} ({start})"
        assert end <= "2025-12-31", f"Found run ending in test split: {r.get('run_id')} ({end})"
