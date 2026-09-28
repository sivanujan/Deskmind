"""Which symbols each service works on, derived from config + the universe table.

- live data + trend agent: DATA_SYMBOLS + universe symbols in state tradeable or watch_only
  (data verified). Exited, blocked and still-downloading symbols are not streamed.
- chart selector: DATA_SYMBOLS + every current universe member (any state) + watch_only, each
  with its state and whether history is on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deskmind.universe.models import SymbolRecord
from deskmind.universe.store import UniverseStore

LIVE_STATES = frozenset({"tradeable", "watch_only"})


def _ordered(recs: dict[str, SymbolRecord], keep: Any) -> list[SymbolRecord]:
    return sorted((r for r in recs.values() if keep(r)), key=lambda r: (r.rank or 10**6, r.symbol))


def live_symbols(configured: list[str], recs: dict[str, SymbolRecord]) -> list[str]:
    out = list(configured)
    for r in _ordered(recs, lambda r: r.state in LIVE_STATES):
        if r.symbol not in out:
            out.append(r.symbol)
    return out


async def active_symbols(configured: list[str], store: UniverseStore) -> list[str]:
    """Live + trend symbols. If the universe table cannot be read, fall back to config."""
    try:
        recs = await store.load()
    except Exception:
        return list(configured)
    return live_symbols(configured, recs)


def has_parquet(data_dir: Path, symbol: str) -> bool:
    root = Path(data_dir) / "parquet" / symbol
    return root.exists() and any(root.rglob("*.parquet"))


def chart_entries(
    configured: list[str], recs: dict[str, SymbolRecord], data_dir: Path
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sym in configured:
        r = recs.get(sym)
        entries.append(
            {
                "symbol": sym,
                "state": r.state if r else None,
                "configured": True,
                "has_data": has_parquet(data_dir, sym),
            }
        )
        seen.add(sym)
    for r in _ordered(recs, lambda r: r.member or r.state == "watch_only"):
        if r.symbol in seen:
            continue
        entries.append(
            {
                "symbol": r.symbol,
                "state": r.state,
                "configured": False,
                "has_data": has_parquet(data_dir, r.symbol),
            }
        )
    return entries
