"""Parquet partition storage for point-in-time feature store using PyArrow."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from deskmind.features.models import FeatureRow
from deskmind.research.split_guard import SplitGuard


class FeatureStore:
    def __init__(self, base_dir: Path | str = "data/features") -> None:
        self.base_dir = Path(base_dir)

    def write_features(self, rows: Sequence[FeatureRow]) -> None:
        """Write feature rows partitioned by symbol and YYYY-MM."""
        if not rows:
            return

        # Group rows by (symbol, year_month)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            d = r.as_dict()
            ym = datetime.fromtimestamp(r.timestamp / 1000, tz=UTC).strftime("%Y-%m")
            grouped.setdefault((r.symbol, ym), []).append(d)

        for (symbol, ym), item_list in grouped.items():
            partition_dir = self.base_dir / f"symbol={symbol}" / f"year_month={ym}"
            partition_dir.mkdir(parents=True, exist_ok=True)
            file_path = partition_dir / "features.parquet"

            table = pa.Table.from_pylist(item_list)
            pq.write_table(table, file_path, compression="snappy")

    def read_features(
        self,
        symbol: str,
        split: str | None = "train",
        year_month: str | None = None,
        hypothesis_id: str | None = None,
        gate_result: Any = None,
    ) -> list[dict[str, Any]]:
        """Read feature dicts for symbol, strictly filtered by split (default='train') or YYYY-MM."""
        if split:
            SplitGuard.validate_split_request(split=split, hypothesis_id=hypothesis_id, gate_result=gate_result)

        sym_dir = self.base_dir / f"symbol={symbol}"
        if not sym_dir.exists():
            return []

        if year_month:
            ym_path = sym_dir / f"year_month={year_month}" / "features.parquet"
            if ym_path.exists():
                table = pq.read_table(ym_path)
                rows = table.to_pylist()
                return SplitGuard.filter_features_by_split(rows, split) if split else rows
            return []

        files = list(sym_dir.glob("year_month=*/features.parquet"))
        if not files:
            return []

        results: list[dict[str, Any]] = []
        for f in sorted(files):
            table = pq.read_table(f)
            results.extend(table.to_pylist())
        results.sort(key=lambda r: r["timestamp"])

        if split:
            results = SplitGuard.filter_features_by_split(results, split)

        return results
