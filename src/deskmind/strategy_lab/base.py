"""Base template and validator for Strategy Lab strategies."""

from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import pandas as pd

from deskmind.strategy_lab.broker import TradingViewBroker


@dataclass
class BarContext:
    """Context passed to strategy.on_bar() at each bar close."""

    i: int
    t_bar: pd.Timestamp
    open_time: pd.Series
    open: float
    high: float
    low: float
    close: float
    volume: float
    in_date: bool
    tick: float
    df: pd.DataFrame
    vol_30d_med: float | None = None


@runtime_checkable
class Strategy(Protocol):
    """Template protocol that every strategy must implement."""

    name: str
    version: str
    timeframe: str  # e.g. "4h", "1d", "15m"
    params: dict[str, Any]

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Vectorized indicator computation across the full history.

        Must use only current and past prices (no look-ahead).
        """
        ...

    def on_bar(self, i: int, ctx: BarContext, broker: TradingViewBroker) -> None:
        """Called at the close of bar i.

        Signals generated here schedule market orders executing at bar i+1 open.
        """
        ...


ALLOWED_ROOT_MODULES = {
    "numpy",
    "pandas",
    "math",
    "deskmind.strategy_lab",
    "typing",
    "dataclasses",
    "__future__",
}


class StrategyValidationError(ValueError):
    """Raised when a strategy file fails structural or safety validation."""

    pass


def validate_strategy_ast(source_code: str) -> None:
    """Parse strategy AST to enforce import restrictions and Strategy class presence."""
    try:
        tree = ast.parse(source_code)
    except SyntaxError as e:
        raise StrategyValidationError(f"Syntax error in strategy file: {e}") from e

    strategy_node: ast.ClassDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Strategy":
            strategy_node = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                is_lab = alias.name.startswith("deskmind.strategy_lab")
                if root not in ALLOWED_ROOT_MODULES and not is_lab:
                    raise StrategyValidationError(
                        f"Forbidden import: '{alias.name}'. Strategies may only import "
                        f"from numpy, pandas, math, and deskmind.strategy_lab."
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            root = node.module.split(".")[0]
            is_lab = node.module.startswith("deskmind.strategy_lab")
            if root not in ALLOWED_ROOT_MODULES and not is_lab:
                raise StrategyValidationError(
                    f"Forbidden import: 'from {node.module} import ...'. Strategies may only "
                    f"import from numpy, pandas, math, and deskmind.strategy_lab."
                )

    if strategy_node is None:
        raise StrategyValidationError("Strategy file must define a 'Strategy' class")

    # Inspect Strategy class for required fields and methods
    defined_names: set[str] = set()
    for item in strategy_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined_names.add(item.name)
            if item.name == "__init__":
                for sub in ast.walk(item):
                    if (
                        isinstance(sub, ast.Attribute)
                        and isinstance(sub.value, ast.Name)
                        and sub.value.id == "self"
                    ):
                        defined_names.add(sub.attr)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            defined_names.add(item.target.id)
        elif isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    defined_names.add(t.id)

    required = ["name", "version", "timeframe", "params", "indicators", "on_bar"]
    missing = [req for req in required if req not in defined_names]
    if missing:
        raise StrategyValidationError(
            f"Strategy class missing required attributes or methods: {missing}"
        )


def load_strategy(file_path: Path | str) -> Strategy:
    """Load and validate a Strategy file.

    Checks:
      1. Source code imports only allowed modules (numpy, pandas, math, deskmind.strategy_lab).
      2. The module defines a `Strategy` class.
      3. Required fields (name, version, timeframe, params) and methods are present.

    Returns:
      Instantiated Strategy object.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Strategy file not found: {path}")

    source = path.read_text(encoding="utf-8")
    validate_strategy_ast(source)

    mod_name = f"strategy_lab_user_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module spec from {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "Strategy"):
        raise StrategyValidationError(f"Strategy file {path.name} must define a 'Strategy' class")

    cls = module.Strategy
    instance = cls() if isinstance(cls, type) else cls

    # Validate required fields
    required_attrs = ["name", "version", "timeframe", "params"]
    missing = [attr for attr in required_attrs if not hasattr(instance, attr)]
    if missing:
        raise StrategyValidationError(f"Strategy class missing required attributes: {missing}")

    if not callable(getattr(instance, "indicators", None)):
        raise StrategyValidationError(
            "Strategy class must implement callable method 'indicators(df)'"
        )

    if not callable(getattr(instance, "on_bar", None)):
        raise StrategyValidationError(
            "Strategy class must implement callable method 'on_bar(i, ctx, broker)'"
        )

    return instance


def run_strategy_backtest(
    strategy: Strategy,
    df: pd.DataFrame,
    tick: float,
    start: str | None = None,
    end: str | None = None,
    broker: TradingViewBroker | None = None,
    min_quote_volume_30d: float | None = None,
    tiered_slippage: bool = False,
) -> dict[str, Any]:
    """Execute a strategy backtest over historical OHLCV data."""
    if broker is None:
        init_cap = strategy.params.get("initial_capital", 10_000.0)
        eq_pct = strategy.params.get("equity_pct_per_trade", 10.0)
        comm_pct = strategy.params.get("commission_pct", 0.05)
        slip_ticks = strategy.params.get("slippage_ticks", 1)
        allow_rev = strategy.params.get("allow_reverse", True)

        broker = TradingViewBroker(
            tick=tick,
            initial_capital=init_cap,
            equity_pct_per_trade=eq_pct,
            commission_pct=comm_pct,
            slippage_ticks=slip_ticks,
            allow_reverse=allow_rev,
            min_quote_volume_30d=min_quote_volume_30d,
            tiered_slippage=tiered_slippage,
        )

    # 1. Compute indicators across full history
    enriched_df = strategy.indicators(df.reset_index(drop=True))

    t_open = pd.to_datetime(enriched_df["open_time"], utc=True)
    open_arr = enriched_df["open"].to_numpy(float)
    high_arr = enriched_df["high"].to_numpy(float)
    low_arr = enriched_df["low"].to_numpy(float)
    close_arr = enriched_df["close"].to_numpy(float)
    vol_arr = (
        enriched_df["vol_30d_med"].to_numpy(float)
        if "vol_30d_med" in enriched_df.columns
        else None
    )

    t_start = pd.Timestamp(start, tz="UTC") if start else None
    t_end = pd.Timestamp(end, tz="UTC") if end else None
    n = len(enriched_df)

    # 2. Main bar loop
    for i in range(n):
        # A. Next-bar open market execution
        broker.execute_bar_open(i, open_arr, close_arr, t_open, vol_arr)

        # B. Intrabar price path stop / limit checks
        broker.execute_bar_intrabar(i, open_arr, high_arr, low_arr, close_arr, t_open, vol_arr)

        # C. Call strategy at bar close
        t_bar = t_open[i]
        in_date = (t_start is None or t_bar >= t_start) and (t_end is None or t_bar <= t_end)
        bar_vol = vol_arr[i] if vol_arr is not None else None

        ctx = BarContext(
            i=i,
            t_bar=t_bar,
            open_time=t_open,
            open=open_arr[i],
            high=high_arr[i],
            low=low_arr[i],
            close=close_arr[i],
            volume=float(enriched_df["volume"].iloc[i]) if "volume" in enriched_df else 1.0,
            in_date=in_date,
            tick=tick,
            df=enriched_df,
            vol_30d_med=bar_vol,
        )

        strategy.on_bar(i, ctx, broker)

        # D. Mark to market equity at bar close
        broker.record_bar_close(i, close_arr, t_bar, t_end)

    return broker.get_results(t_open)
