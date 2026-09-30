"""Point-in-time universe selector for research runs."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from deskmind.universe.history import MembershipHistory, MembershipRow


def select_point_in_time_symbols(
    history: MembershipHistory,
    start_date: date,
    end_date: date,
    top_n: int = 10,
) -> list[str]:
    """Select the union of top N universe symbols by rank on each date in range."""
    symbols: set[str] = set()
    cur = start_date
    while cur <= end_date:
        members = history.get_tradeable(cur) if hasattr(history, "get_tradeable") else history.get_universe(cur)
        sorted_members = sorted(members, key=lambda m: getattr(m, "rank", 999))
        for m in sorted_members[:top_n]:
            sym = m.symbol if hasattr(m, "symbol") else str(m)
            symbols.add(sym)
        cur += timedelta(days=1)

    return sorted(symbols)
