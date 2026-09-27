"""Overlay contract (schema version 1): what agents draw on the chart.

Shared with the frontend: the TypeScript mirror lives in apps/web/src/lib/overlays.ts and the
generated JSON Schema in docs/overlay.schema.json (a test keeps it in sync with these models;
regenerate with `uv run python -m deskmind.api.overlays > docs/overlay.schema.json`).

Three drawing primitives, discriminated by `type`:
- marker: a point on one candle (swing high/low, BOS, CHoCH, ...)
- line:   a segment between two (time, price) points (trend lines, levels)
- zone:   a price box between two times (order blocks, FVGs, ranges)
Every overlay carries `source_agent`, `agent_version` and a semantic `kind` (e.g. "swing_high",
"bos", "trendline", "order_block"); optional `confirmed_at` (when it became knowable) and `meta`
(hover details) were added in a backward-compatible way. Times are UNIX seconds (UTC), prices
are floats (display only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

OVERLAY_SCHEMA_VERSION = 1

HexColor = Annotated[
    str, Field(pattern=r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$", examples=["#26a69a"])
]


class OverlayBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable id, unique per (source_agent, symbol, timeframe)")
    kind: str = Field(description="Semantic kind, e.g. swing_high, bos, choch, trendline")
    source_agent: str = Field(description="Agent that produced it, e.g. trend")
    agent_version: str = Field(description="Version of that agent's logic")
    schema_version: Literal[1] = OVERLAY_SCHEMA_VERSION
    confirmed_at: int | None = Field(
        None, description="UNIX seconds when the item became knowable (replay shows <= T only)"
    )
    meta: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, description="Details shown on hover (rule, times, levels)"
    )


class MarkerOverlay(OverlayBase):
    type: Literal["marker"] = "marker"
    time: int = Field(description="Candle open time, UNIX seconds")
    position: Literal["above", "below"]
    shape: Literal["circle", "square", "arrowUp", "arrowDown"] = "circle"
    color: HexColor = "#f5c542"
    text: str = ""


class LineOverlay(OverlayBase):
    type: Literal["line"] = "line"
    start_time: int
    start_price: float
    end_time: int
    end_price: float
    style: Literal["solid", "dashed", "dotted"] = "solid"
    color: HexColor = "#5b9cf6"
    label: str = ""


class ZoneOverlay(OverlayBase):
    type: Literal["zone"] = "zone"
    start_time: int
    end_time: int
    top: float
    bottom: float
    color: HexColor = Field("#5b9cf633", description="Fill colour, #rrggbb or #rrggbbaa")
    label: str = ""


Overlay = Annotated[MarkerOverlay | LineOverlay | ZoneOverlay, Field(discriminator="type")]


class OverlayResponse(BaseModel):
    symbol: str
    timeframe: str
    schema_version: Literal[1] = OVERLAY_SCHEMA_VERSION
    overlays: list[Overlay] = Field(default_factory=list)


def json_schema() -> dict:
    return TypeAdapter(OverlayResponse).json_schema()


def load_backtest_overlays(
    run_id: str,
    symbol: str,
    start: int | None = None,
    end: int | None = None,
    as_of: int | None = None,
    reports_dir: Path = Path("reports/backtests"),
) -> list[Overlay]:
    overlay_path = reports_dir / run_id / "overlays.json"
    if not overlay_path.exists():
        return []
    try:
        with open(overlay_path, encoding="utf-8") as f:
            raw = json.load(f)
        adapter = TypeAdapter(list[Overlay])
        all_overlays = adapter.validate_python(raw)
        filtered: list[Overlay] = []
        for o in all_overlays:
            if o.meta.get("symbol") != symbol:
                continue
            if as_of is not None and o.confirmed_at is not None and o.confirmed_at > as_of:
                continue
            t = getattr(o, "time", None) or getattr(o, "start_time", None)
            if t is not None:
                if start is not None and t < start:
                    continue
                if end is not None and t >= end:
                    continue
            filtered.append(o)
        return filtered
    except Exception:
        return []


async def load_execution_overlays(
    symbol: str,
    start: int | None = None,
    end: int | None = None,
    as_of: int | None = None,
) -> list[Overlay]:
    """Load execution trade overlays from PostgreSQL trade_journal table.

    Converts executed entries and exits into chart MarkerOverlays.
    """
    overlays: list[Overlay] = []
    try:
        from sqlalchemy import select

        from deskmind.db.models import TradeJournal
        from deskmind.db.session import session_scope

        async with session_scope() as session:
            stmt = select(TradeJournal).where(TradeJournal.symbol == symbol)
            res = await session.execute(stmt)
            trades = res.scalars().all()

        for t in trades:
            mode = t.trading_mode or "live"
            src = f"exec:{mode}"
            entry_time = int(t.created_at.timestamp())

            # Entry marker
            if (as_of is None or entry_time <= as_of) and (
                (start is None or entry_time >= start) and (end is None or entry_time < end)
            ):
                overlays.append(
                    MarkerOverlay(
                        id=f"exec_{t.id}_entry",
                        kind="trade_entry",
                        source_agent=src,
                        agent_version="1.0.0",
                        schema_version=1,
                        confirmed_at=entry_time,
                        time=entry_time,
                        position="below" if t.side == "long" else "above",
                        shape="arrowUp" if t.side == "long" else "arrowDown",
                        color="#22c55e" if t.side == "long" else "#ef4444",
                        text=f"{t.side.upper()} {t.size} @ {t.entry}",
                        meta={
                            "symbol": t.symbol,
                            "price": float(t.entry),
                            "cid": t.client_order_id or "",
                            "trading_mode": mode,
                        },
                    )
                )

            # Exit marker (if closed)
            exit_dt = t.exit_time
            if t.status == "closed" and exit_dt is not None and t.exit_price is not None:
                exit_time = int(exit_dt.timestamp())
                if (as_of is None or exit_time <= as_of) and (
                    (start is None or exit_time >= start) and (end is None or exit_time < end)
                ):
                    overlays.append(
                        MarkerOverlay(
                            id=f"exec_{t.id}_exit",
                            kind="trade_exit",
                            source_agent=src,
                            agent_version="1.0.0",
                            schema_version=1,
                            confirmed_at=exit_time,
                            time=exit_time,
                            position="above" if t.side == "long" else "below",
                            shape="circle",
                            color="#38bdf8",
                            text=f"EXIT ({t.exit_reason or 'close'}) PnL: ${t.pnl:.2f}",
                            meta={
                                "symbol": t.symbol,
                                "price": float(t.exit_price),
                                "pnl": float(t.pnl),
                                "cid": t.client_order_id or "",
                                "trading_mode": mode,
                            },
                        )
                    )
    except Exception:
        # Fallback gracefully if database is not reachable
        pass

    return overlays


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, sort_keys=True))


EXTERNAL_SOURCE = "binance:history"


async def load_external_trade_overlays(
    symbol: str,
    interval_ms: int,
    start: int | None = None,
    end: int | None = None,
    limit: int = 2000,
) -> list[Overlay]:
    """The user's own imported Binance fills (external_trades) as chart markers.

    Fills are grouped per candle and side (one marker per bar and direction) so a busy bar stays
    readable; the hover details list the count, total qty, average price, fees and realized PnL.
    """
    from collections import defaultdict
    from datetime import UTC, datetime
    from decimal import Decimal

    from deskmind.data.external_trades import list_trades
    from deskmind.db.session import session_scope

    try:
        async with session_scope() as session:
            trades = await list_trades(
                session,
                symbol=symbol,
                start=datetime.fromtimestamp(start, UTC) if start is not None else None,
                end=datetime.fromtimestamp(end, UTC) if end is not None else None,
                limit=limit,
            )
    except Exception:
        return []
    groups: dict[tuple[int, str, str], list] = defaultdict(list)
    for t in trades:
        bar = int(t.time.timestamp() * 1000) // interval_ms * interval_ms
        groups[(bar, t.side, t.market)].append(t)
    out: list[Overlay] = []
    for (bar, side, market), fills in sorted(groups.items()):
        qty = sum((f.qty for f in fills), Decimal(0))
        notional = sum((f.price * f.qty for f in fills), Decimal(0))
        pnl = [f.realized_pnl for f in fills if f.realized_pnl is not None]
        fees: dict[str, Decimal] = defaultdict(Decimal)
        for f in fills:
            if f.fee is not None and f.fee_asset:
                fees[f.fee_asset] += f.fee
        buy = side == "BUY"
        out.append(
            MarkerOverlay(
                id=f"{EXTERNAL_SOURCE}:{market}:{side}:{bar}",
                kind=f"binance_{side.lower()}",
                source_agent=EXTERNAL_SOURCE,
                agent_version="import",
                time=bar // 1000,
                confirmed_at=int(max(f.time for f in fills).timestamp()),
                position="below" if buy else "above",
                shape="arrowUp" if buy else "arrowDown",
                color="#2196f3" if buy else "#ff9800",
                text=f"{'B' if buy else 'S'}{'' if len(fills) == 1 else f'x{len(fills)}'}",
                meta={
                    "source": "your Binance history (read-only import)",
                    "market": market,
                    "side": side,
                    "fills": len(fills),
                    "qty": str(qty),
                    "avg_price": str((notional / qty).quantize(Decimal("0.00000001")))
                    if qty
                    else None,
                    "fees": ", ".join(f"{v} {k}" for k, v in fees.items()) or None,
                    "realized_pnl": str(sum(pnl, Decimal(0))) if pnl else None,
                    "first_fill": min(f.time for f in fills).isoformat(),
                },
            )
        )
    return out
