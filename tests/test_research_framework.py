"""Tests for Event Study tool and Research Gates validation."""

from __future__ import annotations

import pytest

from deskmind.agents.trend.models import Bar
from deskmind.research.event_study import calculate_event_study, generate_matched_random_baseline
from deskmind.research.gates import calculate_deflated_sharpe, evaluate_research_gates
from deskmind.research.hypotheses.models import Signal


def test_event_study_calculation() -> None:
    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1000,
        direction="bullish",
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        r_multiple=2.0,
        rationale="Test bullish signal",
    )

    # 48 bars going up
    forward_bars = [
        Bar(open_time=1000 + i * 300_000, close_time=1000 + (i + 1) * 300_000, open=100.0 + i, high=101.0 + i, low=99.5 + i, close=100.5 + i)
        for i in range(1, 49)
    ]

    res = calculate_event_study(sig, forward_bars)
    assert res.bars_evaluated == 48
    assert res.mfe_r[48] > 0.0
    assert res.target_or_stop_first in ("target", "stop", "neither")


def test_research_gates_evaluation() -> None:
    sig = Signal(
        hypothesis_id="H1_v1.0.0",
        symbol="BTCUSDT",
        timestamp=1000,
        direction="bullish",
        entry_price=100.0,
        stop_price=90.0,
        target_price=120.0,
        r_multiple=2.0,
        rationale="Test signal",
    )
    forward_bars = [
        Bar(open_time=1000 + i * 300_000, close_time=1000 + (i + 1) * 300_000, open=100.0 + i, high=101.0 + i, low=99.5 + i, close=100.5 + i)
        for i in range(1, 49)
    ]

    es_res = calculate_event_study(sig, forward_bars)
    results = [es_res] * 120  # 120 identical winning signals

    gate_res = evaluate_research_gates("H1_v1.0.0", results, results, trial_count=1)
    assert gate_res.trades_count == 120
    assert gate_res.gross_expectancy_r > 0.15
    assert gate_res.checklist["min_trades_100"] is True
    assert gate_res.checklist["gross_expectancy_0_15R"] is True


def test_deflated_sharpe_calculation() -> None:
    returns = [0.5, 1.2, -0.8, 1.5, 0.4, 0.9, -0.2, 1.1, 0.7, -0.5]
    dsr = calculate_deflated_sharpe(returns, total_trials=3)
    assert isinstance(dsr, float)


def test_event_study_train_split_never_reads_2025_row(tmp_path) -> None:
    """Event study on 'train' split never reads a 2025 feature row."""
    from datetime import UTC, datetime
    from deskmind.features.builder import build_feature_row
    from deskmind.features.store import FeatureStore

    store = FeatureStore(base_dir=tmp_path)
    # 2024-12-31 timestamp (train split)
    t_2024 = int(datetime(2024, 12, 31, 23, 0, tzinfo=UTC).timestamp() * 1000)
    # 2025-01-01 timestamp (validation split)
    t_2025 = int(datetime(2025, 1, 1, 1, 0, tzinfo=UTC).timestamp() * 1000)

    row_2024 = build_feature_row("BTCUSDT", t_2024, None, None, None, None, None, None, None, None, None, None, 65000.0)
    row_2025 = build_feature_row("BTCUSDT", t_2025, None, None, None, None, None, None, None, None, None, None, 66000.0)

    store.write_features([row_2024, row_2025])

    # Read on train split
    train_rows = store.read_features("BTCUSDT", split="train")
    assert len(train_rows) == 1
    assert train_rows[0]["timestamp"] == t_2024


def test_validation_split_without_passing_gates_refused() -> None:
    """Running on 'validation' split without passing gates is refused."""
    from deskmind.research.split_guard import SplitGuard

    # Without gate result
    with pytest.raises(PermissionError, match="refused"):
        SplitGuard.validate_split_request(split="validation", hypothesis_id="H1_v1.0.0", gate_result=None)

    # Test split permanently locked
    with pytest.raises(PermissionError, match="locked"):
        SplitGuard.validate_split_request(split="test", hypothesis_id="H1_v1.0.0")

