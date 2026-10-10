"""Strategy Lab rules loader and dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class UniverseRules:
    path: str
    excluded_symbols: list[str] = field(default_factory=list)


@dataclass
class LiquidityRules:
    min_30d_median_quote_volume_usd: float
    volume_data_path: str


@dataclass
class SlippageTier:
    min_volume: float
    ticks: int = 1
    pct: float = 0.0
    min_ticks: int = 1


@dataclass
class SlippageRules:
    tiered: bool
    tiers: list[SlippageTier] = field(default_factory=list)


@dataclass
class SplitWindow:
    name: str
    start: str
    end: str | None = None
    locked: bool = False


@dataclass
class SplitsRules:
    train: SplitWindow
    validation: SplitWindow
    test: SplitWindow


@dataclass
class TrainSelectionRules:
    min_positions: int
    min_profit_factor: float
    min_mean_r: float
    max_drawdown_pct: float


@dataclass
class ValidationPassRules:
    min_pooled_mean_r: float
    min_pooled_profit_factor: float
    min_profitable_cohort_pct: float


@dataclass
class StrategyLabRules:
    version: str
    name: str
    description: str
    universe: UniverseRules
    liquidity: LiquidityRules
    slippage: SlippageRules
    splits: SplitsRules
    train_selection: TrainSelectionRules
    validation_pass: ValidationPassRules


def load_rules(version_or_path: str = "v1") -> StrategyLabRules:
    """Load and parse strategy rules YAML file (default: rules_v1.yaml)."""
    if version_or_path == "v1":
        candidates = [
            Path("strategy_lab/rules_v1.yaml"),
            Path(__file__).parent / "rules_v1.yaml",
        ]
        path = next((p for p in candidates if p.exists()), candidates[0])
    else:
        path = Path(version_or_path)

    if not path.exists():
        raise FileNotFoundError(f"Rules file not found: {path}")

    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    tiers = [
        SlippageTier(
            min_volume=t.get("min_volume", 0.0),
            ticks=t.get("ticks", 1),
            pct=t.get("pct", 0.0),
            min_ticks=t.get("min_ticks", 1),
        )
        for t in data.get("slippage", {}).get("tiers", [])
    ]

    splits_data = data.get("splits", {})
    return StrategyLabRules(
        version=data.get("version", "v1"),
        name=data.get("name", "Standard_Rules"),
        description=data.get("description", ""),
        universe=UniverseRules(
            path=data.get("universe", {}).get("path", "reports/research/universe_2022_2025.json"),
            excluded_symbols=data.get("universe", {}).get("excluded_symbols", []),
        ),
        liquidity=LiquidityRules(
            min_30d_median_quote_volume_usd=data.get("liquidity", {}).get(
                "min_30d_median_quote_volume_usd", 20_000_000.0
            ),
            volume_data_path=data.get("liquidity", {}).get(
                "volume_data_path", "data/daily_quote_volume.parquet"
            ),
        ),
        slippage=SlippageRules(
            tiered=data.get("slippage", {}).get("tiered", True),
            tiers=tiers,
        ),
        splits=SplitsRules(
            train=SplitWindow(
                name=splits_data.get("train", {}).get("name", "TRAIN"),
                start=splits_data.get("train", {}).get("start", "2023-01-01"),
                end=splits_data.get("train", {}).get("end", "2024-12-31"),
            ),
            validation=SplitWindow(
                name=splits_data.get("validation", {}).get("name", "VALIDATION"),
                start=splits_data.get("validation", {}).get("start", "2025-01-01"),
                end=splits_data.get("validation", {}).get("end", "2025-12-31"),
            ),
            test=SplitWindow(
                name=splits_data.get("test", {}).get("name", "TEST"),
                start=splits_data.get("test", {}).get("start", "2026-01-01"),
                end=splits_data.get("test", {}).get("end"),
                locked=splits_data.get("test", {}).get("locked", True),
            ),
        ),
        train_selection=TrainSelectionRules(
            min_positions=int(data.get("train_selection", {}).get("min_positions", 30)),
            min_profit_factor=float(data.get("train_selection", {}).get("min_profit_factor", 1.30)),
            min_mean_r=float(data.get("train_selection", {}).get("min_mean_r", 0.0)),
            max_drawdown_pct=float(
                data.get("train_selection", {}).get("max_drawdown_pct", 20.0)
            ),
        ),
        validation_pass=ValidationPassRules(
            min_pooled_mean_r=float(
                data.get("validation_pass", {}).get("min_pooled_mean_r", 0.0)
            ),
            min_pooled_profit_factor=float(
                data.get("validation_pass", {}).get("min_pooled_profit_factor", 1.20)
            ),
            min_profitable_cohort_pct=float(
                data.get("validation_pass", {}).get("min_profitable_cohort_pct", 60.0)
            ),
        ),
    )


@dataclass
class WalkForwardQuarter:
    id: str
    label: str
    train_start: str
    train_end: str
    test_start: str
    test_end: str


@dataclass
class WalkForwardVerdictRules:
    min_pooled_mean_r: float
    min_pooled_profit_factor: float
    min_profitable_quarters: int
    total_quarters: int


@dataclass
class WalkForwardRules:
    version: str
    name: str
    description: str
    universe: UniverseRules
    liquidity: LiquidityRules
    slippage: SlippageRules
    selection_rules: TrainSelectionRules
    verdict_rules: WalkForwardVerdictRules
    quarters: list[WalkForwardQuarter]
    test_2026_locked: bool = True


def load_walkforward_rules(version_or_path: str = "walkforward_v1") -> WalkForwardRules:
    """Load and parse walk-forward rules YAML file (default: rules_walkforward_v1.yaml)."""
    if version_or_path in ("walkforward_v1", "v1"):
        candidates = [
            Path("strategy_lab/rules_walkforward_v1.yaml"),
            Path(__file__).parent / "rules_walkforward_v1.yaml",
        ]
        path = next((p for p in candidates if p.exists()), candidates[0])
    else:
        path = Path(version_or_path)

    if not path.exists():
        raise FileNotFoundError(f"Walk-forward rules file not found: {path}")

    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))

    tiers = [
        SlippageTier(
            min_volume=t.get("min_volume", 0.0),
            ticks=t.get("ticks", 1),
            pct=t.get("pct", 0.0),
            min_ticks=t.get("min_ticks", 1),
        )
        for t in data.get("slippage", {}).get("tiers", [])
    ]

    quarters = [
        WalkForwardQuarter(
            id=q["id"],
            label=q.get("label", q["id"]),
            train_start=q["train_start"],
            train_end=q["train_end"],
            test_start=q["test_start"],
            test_end=q["test_end"],
        )
        for q in data.get("quarters", [])
    ]

    sel_data = data.get("selection_rules", {})
    verdict_data = data.get("verdict_rules", {})

    return WalkForwardRules(
        version=data.get("version", "walkforward_v1"),
        name=data.get("name", "TAS_WalkForward_Standard_Rules"),
        description=data.get("description", ""),
        universe=UniverseRules(
            path=data.get("universe", {}).get("path", "reports/research/universe_2022_2025.json"),
            excluded_symbols=data.get("universe", {}).get("excluded_symbols", []),
        ),
        liquidity=LiquidityRules(
            min_30d_median_quote_volume_usd=data.get("liquidity", {}).get(
                "min_30d_median_quote_volume_usd", 20_000_000.0
            ),
            volume_data_path=data.get("liquidity", {}).get(
                "volume_data_path", "data/daily_quote_volume.parquet"
            ),
        ),
        slippage=SlippageRules(
            tiered=data.get("slippage", {}).get("tiered", True),
            tiers=tiers,
        ),
        selection_rules=TrainSelectionRules(
            min_positions=int(sel_data.get("min_positions", 15)),
            min_profit_factor=float(sel_data.get("min_profit_factor", 1.30)),
            min_mean_r=float(sel_data.get("min_mean_r", 0.0)),
            max_drawdown_pct=float(sel_data.get("max_drawdown_pct", 20.0)),
        ),
        verdict_rules=WalkForwardVerdictRules(
            min_pooled_mean_r=float(verdict_data.get("min_pooled_mean_r", 0.0)),
            min_pooled_profit_factor=float(verdict_data.get("min_pooled_profit_factor", 1.20)),
            min_profitable_quarters=int(verdict_data.get("min_profitable_quarters", 5)),
            total_quarters=int(verdict_data.get("total_quarters", 8)),
        ),
        quarters=quarters,
        test_2026_locked=bool(data.get("test_2026", {}).get("locked", True)),
    )
