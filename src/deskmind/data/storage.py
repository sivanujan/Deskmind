"""Parquet storage, manifest and DuckDB helpers for historical market data.

Layout (under settings.data_dir):
    parquet/{symbol}/klines/{tf}/year=YYYY/month=MM/part.parquet
    parquet/{symbol}/mark_klines/{tf}/year=YYYY/month=MM/part.parquet
    parquet/{symbol}/funding/year=YYYY/part.parquet
    parquet/{symbol}/_manifest.json
    meta/{symbol}_filters_{YYYY-MM-DD}.json

Prices and volumes are float64 here for analytics speed. This data is for research and backtests
only: execution, risk and sizing code must work in Decimal, using exchange filters (tickSize,
stepSize) from the meta snapshot, never these floats.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from deskmind.data.datasets import Dataset, from_ms, normalize_funding_ms, to_ms

TS = pa.timestamp("ms", tz="UTC")

KLINE_SCHEMA = pa.schema(
    [
        ("open_time", TS),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("close_time", TS),
        ("quote_volume", pa.float64()),
        ("trades", pa.int64()),
        ("taker_buy_volume", pa.float64()),
        ("taker_buy_quote_volume", pa.float64()),
    ]
)
MARK_KLINE_SCHEMA = pa.schema(
    [
        ("open_time", TS),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("close_time", TS),
    ]
)
FUNDING_SCHEMA = pa.schema(
    [
        ("funding_time", TS),
        ("funding_rate", pa.float64()),
        ("mark_price", pa.float64()),  # REST only; null for bulk rows
        ("funding_interval_hours", pa.int32()),  # bulk only; null for REST rows
    ]
)


def schema_for(ds: Dataset) -> pa.Schema:
    return {"klines": KLINE_SCHEMA, "mark_klines": MARK_KLINE_SCHEMA, "funding": FUNDING_SCHEMA}[
        ds.kind
    ]


def time_key(ds: Dataset) -> str:
    return "funding_time" if ds.kind == "funding" else "open_time"


# ---------------------------------------------------------------- row parsing


def _ms(v: Any) -> int:
    t = int(v)
    return t // 1000 if t >= 10**15 else t  # tolerate microsecond timestamps


def _ts_array(values: Sequence[int]) -> pa.Array:
    return pa.array(values, pa.int64()).cast(TS)


def candles_table(ds: Dataset, rows: Iterable[Sequence[Any]]) -> pa.Table:
    """Binance kline rows (CSV strings or REST JSON lists, 12 fields) -> Arrow table."""
    rows = list(rows)
    if not rows:
        return schema_for(ds).empty_table()
    cols = list(zip(*rows, strict=False))

    def f64(i: int) -> pa.Array:
        return pa.array([float(x) for x in cols[i]], pa.float64())

    base = {
        "open_time": _ts_array([_ms(x) for x in cols[0]]),
        "open": f64(1),
        "high": f64(2),
        "low": f64(3),
        "close": f64(4),
        "close_time": _ts_array([_ms(x) for x in cols[6]]),
    }
    if ds.kind == "klines":
        base |= {
            "volume": f64(5),
            "quote_volume": f64(7),
            "trades": pa.array([int(x) for x in cols[8]], pa.int64()),
            "taker_buy_volume": f64(9),
            "taker_buy_quote_volume": f64(10),
        }
    schema = schema_for(ds)
    return pa.table({name: base[name] for name in schema.names}, schema=schema)


def funding_table(
    times_ms: Sequence[int],
    rates: Sequence[float],
    mark_prices: Sequence[float | None] | None = None,
    interval_hours: Sequence[int | None] | None = None,
) -> pa.Table:
    n = len(times_ms)
    return pa.table(
        {
            "funding_time": _ts_array([normalize_funding_ms(_ms(t)) for t in times_ms]),
            "funding_rate": pa.array(rates, pa.float64()),
            "mark_price": pa.array(mark_prices or [None] * n, pa.float64()),
            "funding_interval_hours": pa.array(interval_hours or [None] * n, pa.int32()),
        },
        schema=FUNDING_SCHEMA,
    )


# ---------------------------------------------------------------- table ops


def time_values_ms(table: pa.Table, key: str) -> list[int]:
    return table.column(key).cast(pa.int64()).to_pylist()


def merge_tables(ds: Dataset, *tables: pa.Table | None) -> pa.Table:
    """Concatenate, drop duplicate timestamps (first occurrence wins), sort ascending."""
    parts = [t.select(schema_for(ds).names).cast(schema_for(ds)) for t in tables if t is not None]
    if not parts:
        return schema_for(ds).empty_table()
    combined = pa.concat_tables(parts)
    key = time_key(ds)
    seen: set[int] = set()
    keep: list[int] = []
    for i, t in enumerate(time_values_ms(combined, key)):
        if t not in seen:
            seen.add(t)
            keep.append(i)
    deduped = combined.take(pa.array(keep, pa.int64()))
    return deduped.sort_by(key)


def filter_time(ds: Dataset, table: pa.Table, start_ms: int, end_ms: int) -> pa.Table:
    """Rows with start_ms <= time < end_ms."""

    col = table.column(time_key(ds)).cast(pa.int64())
    mask = pc.and_(pc.greater_equal(col, start_ms), pc.less(col, end_ms))
    return table.filter(mask)


# ---------------------------------------------------------------- paths & IO


class Store:
    def __init__(self, data_dir: Path, symbol: str) -> None:
        self.data_dir = Path(data_dir)
        self.symbol = symbol.upper()
        self.root = self.data_dir / "parquet" / self.symbol
        self.meta_dir = self.data_dir / "meta"
        self.reports_dir = self.data_dir / "reports"
        self.manifest_path = self.root / "_manifest.json"

    def dataset_dir(self, ds: Dataset) -> Path:
        return self.root / ds.key

    def partition_path(self, ds: Dataset, year: int, month: int) -> Path:
        if ds.kind == "funding":
            return self.dataset_dir(ds) / f"year={year:04d}" / "part.parquet"
        return self.dataset_dir(ds) / f"year={year:04d}" / f"month={month:02d}" / "part.parquet"

    def files(self, ds: Dataset) -> list[Path]:
        return sorted(self.dataset_dir(ds).glob("**/part.parquet"))

    def glob(self, ds: Dataset) -> str:
        return (self.dataset_dir(ds) / "**" / "part.parquet").as_posix()

    # manifest: {"start": "YYYY-MM-DD", "datasets": {key: {"YYYY-MM": {...}}}}
    def load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {"start": None, "datasets": {}}

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        _atomic_write_bytes(
            self.manifest_path, json.dumps(manifest, indent=2, sort_keys=True).encode()
        )


def read_parquet(path: Path, schema: pa.Schema) -> pa.Table | None:
    if not path.exists():
        return None
    return pq.read_table(path).select(schema.names).cast(schema)


def write_parquet(path: Path, table: pa.Table) -> None:
    """Atomic write: temp file + rename, so a crash never leaves a half-written partition."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> None:
    _atomic_write_bytes(path, json.dumps(obj, indent=2, sort_keys=True).encode())


def describe(store: Store, datasets: Iterable[Dataset]) -> list[dict[str, Any]]:
    """Row count, time range, file count and bytes on disk per dataset."""
    con = duckdb_connect()
    out = []
    for ds in datasets:
        files = store.files(ds)
        info: dict[str, Any] = {
            "dataset": ds.key,
            "files": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "rows": 0,
            "first": None,
            "last": None,
        }
        if files:
            key = time_key(ds)
            rows, first, last = con.execute(
                f"SELECT count(*), epoch_ms(min({key})), epoch_ms(max({key})) "
                f"FROM {scan_sql(store, ds)}"
            ).fetchone()  # type: ignore[misc]
            info |= {"rows": rows, "first": from_ms(first), "last": from_ms(last)}
        out.append(info)
    con.close()
    return out


# ---------------------------------------------------------------- DuckDB


def duckdb_connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    return con


def scan_sql(store: Store, ds: Dataset) -> str:
    """SQL table expression over a dataset's Parquet files."""
    return f"read_parquet('{store.glob(ds)}', hive_partitioning = false)"


def load_klines(
    data_dir: Path,
    symbol: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    *,
    mark: bool = False,
) -> pa.Table:
    """Candles for one symbol/timeframe with start <= open_time < end, sorted by open_time.

    Floats: for research and backtests only (see module docstring).
    """
    store = Store(data_dir, symbol)
    ds = Dataset("mark_klines" if mark else "klines", timeframe)
    if not store.files(ds):
        return schema_for(ds).empty_table()
    where = []
    if start is not None:
        where.append(f"epoch_ms(open_time) >= {to_ms(start)}")
    if end is not None:
        where.append(f"epoch_ms(open_time) < {to_ms(end)}")
    con = duckdb_connect()
    try:
        tbl = con.execute(
            f"SELECT * FROM {scan_sql(store, ds)} "
            f"{'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY open_time"
        ).arrow()
    finally:
        con.close()
    if isinstance(tbl, pa.RecordBatchReader):
        tbl = tbl.read_all()
    return tbl.select(schema_for(ds).names).cast(schema_for(ds))


def create_view(con: duckdb.DuckDBPyConnection, name: str, store: Store, ds: Dataset) -> bool:
    """View over the dataset with an `_ord` column = physical row order (file path, row number)."""
    if not store.files(ds):
        return False
    con.execute(
        f"""
        CREATE OR REPLACE VIEW {name} AS
        SELECT * EXCLUDE (filename, file_row_number),
               row_number() OVER (ORDER BY filename, file_row_number) AS _ord
        FROM read_parquet('{store.glob(ds)}', hive_partitioning = false,
                          filename = true, file_row_number = true)
        """
    )
    return True


def create_view_from_arrow(con: duckdb.DuckDBPyConnection, name: str, table: pa.Table) -> None:
    """Same shape as create_view, for in-memory tables (tests)."""
    with_ord = table.append_column("_ord", pa.array(range(1, table.num_rows + 1), pa.int64()))
    con.register(f"{name}_arrow", with_ord)
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM {name}_arrow")
