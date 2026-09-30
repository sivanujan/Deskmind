"""Trend items -> chart overlays (overlay contract v1). Items must already be as-of views."""

from __future__ import annotations

from typing import Any

from deskmind.agents.trend.agent import iso
from deskmind.agents.trend.config import AGENT_NAME
from deskmind.api.overlays import LineOverlay, MarkerOverlay, Overlay

LABEL_COLOR = {"HH": "#26a69a", "HL": "#4dd0e1", "LH": "#ffa726", "LL": "#ef5350", None: "#9598a1"}
BREAK_COLOR = {"BOS": "#5b9cf6", "CHoCH": "#f5c542"}
LINE_COLOR = {"support": "#26a69a", "resistance": "#ef5350"}
SWEEP_COLOR = "#b388ff"


def _s(ms: int | None) -> int | None:
    return None if ms is None else ms // 1000


def _meta(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "rule": item.get("rule"),
        "pivot_time": iso(item["pivot_time"]),
        "confirmed_at": iso(item["confirmed_at"]),
    }
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


def to_overlays(items: list[dict[str, Any]], agent_version: str, view_end_ms: int) -> list[Overlay]:
    """view_end_ms: open time of the newest candle on screen (active lines extend to it)."""
    out: list[Overlay] = []
    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}
    for it in items:
        kind = it["kind"]
        if kind in ("swing_high", "swing_low"):
            high = kind == "swing_high"
            text = it["label"] or ("H" if high else "L")
            out.append(
                MarkerOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    time=it["pivot_time"] // 1000,
                    confirmed_at=_s(it["confirmed_at"]),
                    position="above" if high else "below",
                    shape="circle",
                    color=LABEL_COLOR.get(it["label"], "#9598a1"),
                    text=text,
                    meta=_meta(
                        it, level=it["price"], label=text, broken_at=iso(it.get("broken_at"))
                    ),
                )
            )
        elif kind in ("bos", "choch"):
            up = it["direction"] == "up"
            color = BREAK_COLOR[it["type"]]
            meta = _meta(
                it,
                level=it["level"],
                close=it["close"],
                broken_swing=iso(it["swing_pivot_time"]),
                direction=it["direction"],
            )
            out.append(
                MarkerOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    time=it["pivot_time"] // 1000,
                    confirmed_at=_s(it["confirmed_at"]),
                    position="below" if up else "above",
                    shape="arrowUp" if up else "arrowDown",
                    color=color,
                    text=it["type"],
                    meta=meta,
                )
            )
            out.append(
                LineOverlay(
                    **common,
                    id=f"{it['id']}:level",
                    kind=f"{kind}_level",
                    start_time=it["swing_pivot_time"] // 1000,
                    start_price=it["level"],
                    end_time=it["pivot_time"] // 1000,
                    end_price=it["level"],
                    confirmed_at=_s(it["confirmed_at"]),
                    style="dotted",
                    color=color,
                    meta=meta,
                )
            )
        elif kind == "sweep":
            up = it["direction"] == "up"
            out.append(
                MarkerOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    time=it["pivot_time"] // 1000,
                    confirmed_at=_s(it["confirmed_at"]),
                    position="above" if up else "below",
                    shape="square",
                    color=SWEEP_COLOR,
                    text="SWEEP",
                    meta=_meta(
                        it,
                        level=it["level"],
                        wick=it["extreme"],
                        close=it["close"],
                        swept_swing=iso(it["swing_pivot_time"]),
                    ),
                )
            )
        elif kind.startswith("trendline"):
            side = it["kind"].split("_", 1)[1]
            ended = it.get("broken_time") or it.get("expired_at")
            end_ms = min(ended or view_end_ms, view_end_ms)
            if end_ms <= it["a_time"]:
                continue
            slope = it["slope_per_ms"]
            status = (
                "broken" if it.get("broken_at") else "expired" if it.get("expired_at") else "active"
            )
            color = LINE_COLOR[side] + ("" if status == "active" else "59")
            out.append(
                LineOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    start_time=it["a_time"] // 1000,
                    start_price=it["a_price"],
                    end_time=end_ms // 1000,
                    end_price=it["a_price"] + slope * (end_ms - it["a_time"]),
                    confirmed_at=_s(it["confirmed_at"]),
                    style="solid" if status == "active" else "dashed",
                    color=color,
                    label=f"{len(it['touches'])} touches" if status == "active" else "",
                    meta=_meta(
                        it,
                        status=status,
                        touches=len(it["touches"]),
                        touch_times=", ".join(iso(t["pivot_time"]) or "" for t in it["touches"]),
                        tolerance=it["tol"],
                        broken_at=iso(it.get("broken_at")),
                    ),
                )
            )
    return out
