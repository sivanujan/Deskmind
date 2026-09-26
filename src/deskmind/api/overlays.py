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


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2, sort_keys=True))
