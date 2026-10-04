"""Data loader for TAS strategy."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


def get_tick_size(
    symbol: str,
    meta_path: Path | str = "data/meta/symbol_filters_universe.json",
) -> float:
    """Read tick size for a symbol from symbol_filters_universe.json.

    Raises:
        FileNotFoundError: If the metadata file does not exist.
        KeyError: If the symbol or tick_size is missing.
        ValueError: If tick_size cannot be parsed as a float or <= 0.
    """
    path = Path(meta_path)
    if not path.exists():
        raise FileNotFoundError(f"Symbol filters file not found at {path}")

    with path.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    if symbol not in meta:
        raise KeyError(f"Symbol '{symbol}' not found in {path}")

    sym_info = meta[symbol]
    tick_str = sym_info.get("tick_size")
    if tick_str is None:
        raise KeyError(f"Missing 'tick_size' for symbol '{symbol}' in {path}")

    try:
        tick = float(tick_str)
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid tick_size '{tick_str}' for symbol '{symbol}': {e}") from e

    if tick <= 0:
        raise ValueError(f"Tick size must be positive, got {tick} for {symbol}")

    return tick


def load_ohlcv(
    symbol: str,
    tf: str,
    data_dir: Path | str = "data/parquet",
) -> pd.DataFrame:
    """Load native OHLCV klines for a symbol and timeframe from Parquet.

    Uses native parquet storage partitioned by year/month under:
        {data_dir}/{symbol}/klines/{tf}/

    Returns:
        pd.DataFrame sorted by open_time with columns:
        ['open_time', 'open', 'high', 'low', 'close', 'volume', ...]
    """
    base_dir = Path(data_dir) / symbol / "klines" / tf
    if not base_dir.exists():
        raise FileNotFoundError(f"Parquet directory not found at {base_dir}")

    tbl = pq.read_table(str(base_dir))
    df = tbl.to_pandas()

    if df.empty:
        raise ValueError(f"No kline data found for {symbol} {tf} in {base_dir}")

    required_cols = ["open_time", "open", "high", "low", "close"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns {missing} in klines for {symbol} {tf}")

    df = df.sort_values("open_time").reset_index(drop=True)
    return df
