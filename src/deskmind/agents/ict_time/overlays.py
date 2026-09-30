"""Chart overlays for the ICT Time agent (overlay contract v1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from deskmind.agents.ict_time.config import AGENT_NAME
from deskmind.agents.ict_time.engine import (
    _KZ_DEF, _NY, _SB_DEF, _to_ms, _to_ny, LEVEL_LABELS,
)
from deskmind.api.overlays import LineOverlay, MarkerOverlay, Overlay, ZoneOverlay

# Session colours (RGBA)
SESSION_COLORS: dict[str, str] = {
    "asia":      "#7c3aed18",   # violet tint
    "london":    "#0ea5e918",   # sky tint
    "new_york":  "#22c55e18",   # green tint
}
KZ_COLORS: dict[str, str] = {
    "asian":         "#7c3aed25",
    "london_open":   "#0ea5e925",
    "ny_am":         "#22c55e25",
    "ny_lunch":      "#94a3b815",
    "london_close":  "#f9731618",
}
SB_COLOR = "#f59e0b22"


def _s(ms: int | None) -> int | None:
    return None if ms is None else ms // 1000


def _meta(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    created = item.get("created_time", 0)
    meta: dict[str, Any] = {
        "created_time": datetime.fromtimestamp(created / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ) if created else None,
        "confirmed_at": datetime.fromtimestamp(item["confirmed_at"] / 1000, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ) if item.get("confirmed_at") else None,
        "state": item.get("state"),
        "rule": item.get("rule"),
    }
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


def to_overlays(items: list[dict[str, Any]], agent_version: str, view_end_ms: int) -> list[Overlay]:
    """Convert ICT Time items to chart overlays."""
    out: list[Overlay] = []
    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}

    for it in items:
        kind = it.get("kind", "")
        start_ms = it.get("created_time", 0)
        end_ms = view_end_ms

        # ---- Sessions
        if kind.startswith("session_"):
            sess_name = kind[len("session_"):]
            color = SESSION_COLORS.get(sess_name, "#94a3b815")
            if it.get("state") == "closed":
                end_ms = it.get("closed_at") or view_end_ms
            end_ms = min(end_ms, view_end_ms)
            if end_ms <= start_ms:
                end_ms = start_ms + 3_600_000
            out.append(ZoneOverlay(
                **common,
                id=it["id"],
                kind=kind,
                start_time=start_ms // 1000,
                end_time=end_ms // 1000,
                top=it.get("high") or 0.0,
                bottom=it.get("low") or 0.0,
                confirmed_at=_s(it.get("confirmed_at")),
                color=color,
                label=sess_name.replace("_", " ").title(),
                meta=_meta(it, name=sess_name, high=it.get("high"), low=it.get("low")),
            ))

        # ---- Key levels
        elif kind in LEVEL_LABELS:
            price = it.get("price")
            if price is None:
                continue
            state = it.get("state", "active")
            # Choose colour by level type and state
            if kind in ("pdh", "pwh", "pmh", "asian_range_high",
                        "weekly_open", "monthly_open", "daily_open", "ny_midnight_open"):
                base_col = "#ef4444"  # red for resistance-style
            else:
                base_col = "#22c55e"  # green for support-style

            if state == "taken":
                color = base_col + "30"
            elif state == "swept":
                color = base_col + "70"
            else:
                color = base_col

            taken_or_end = it.get("taken_at") or view_end_ms
            end_s = min(taken_or_end, view_end_ms) // 1000
            out.append(LineOverlay(
                **common,
                id=it["id"],
                kind=kind,
                start_time=start_ms // 1000,
                start_price=price,
                end_time=end_s,
                end_price=price,
                confirmed_at=_s(it.get("confirmed_at")),
                style="dashed" if state != "active" else "solid",
                color=color,
                label=LEVEL_LABELS[kind],
                meta=_meta(it, price=price, state=state, swept_by=it.get("swept_by")),
            ))

    return out


def weekend_overlays(
    view_start_s: int,
    view_end_s: int,
    agent_version: str,
) -> list[Overlay]:
    """Generate weekend shading overlays for the view window."""
    from datetime import timedelta

    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}
    out: list[Overlay] = []
    # Walk from view_start to view_end, emit Sat 00:00 UTC → Mon 00:00 UTC zones
    dt = datetime.fromtimestamp(view_start_s, UTC)
    # Rewind to the nearest Saturday before view_start
    days_back = (dt.weekday() - 5) % 7
    sat = (dt - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    while sat.timestamp() < view_end_s:
        mon = sat + timedelta(days=2)
        s_start = max(int(sat.timestamp()), view_start_s)
        s_end = min(int(mon.timestamp()), view_end_s)
        if s_end > s_start:
            out.append(ZoneOverlay(
                **common,
                id=f"weekend:{int(sat.timestamp())}",
                kind="weekend",
                start_time=s_start,
                end_time=s_end,
                top=1e12,
                bottom=0.0,
                confirmed_at=s_start,
                color="#1e293b30",
                label="WKD",
                meta={"note": "Weekend low-liquidity zone"},
            ))
        sat += timedelta(days=7)
    return out


def killzone_overlays(
    view_start_s: int,
    view_end_s: int,
    agent_version: str,
) -> list[Overlay]:
    """Generate killzone band overlays for the view window.

    Killzones repeat daily; we generate one zone per day per killzone name.
    """
    from datetime import timedelta

    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}
    out: list[Overlay] = []
    dt_start = datetime.fromtimestamp(view_start_s, UTC)
    dt_end = datetime.fromtimestamp(view_end_s, UTC)
    # Walk day by day
    day = dt_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < dt_end:
        day_ny = day.astimezone(_NY)
        for kz_name, ((oh, om), (ch, cm)) in _KZ_DEF.items():
            # Asian KZ spans 20:00–24:00 which wraps midnight; emit for the evening
            if kz_name == "asian":
                kz_open = day_ny.replace(hour=20, minute=0, second=0, microsecond=0)
                kz_close = (day_ny + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                kz_open = day_ny.replace(hour=oh, minute=om, second=0, microsecond=0)
                kz_close = day_ny.replace(hour=ch, minute=cm, second=0, microsecond=0)

            ks = max(int(kz_open.timestamp()), view_start_s)
            ke = min(int(kz_close.timestamp()), view_end_s)
            if ke > ks:
                low_prob = kz_name == "ny_lunch"
                out.append(ZoneOverlay(
                    **common,
                    id=f"kz_{kz_name}:{int(kz_open.timestamp())}",
                    kind=f"killzone_{kz_name}",
                    start_time=ks,
                    end_time=ke,
                    top=1e12,
                    bottom=0.0,
                    confirmed_at=ks,
                    color=KZ_COLORS.get(kz_name, "#94a3b818"),
                    label=("LOW" if low_prob else kz_name.upper().replace("_", " ")),
                    meta={"killzone": kz_name, "low_probability": low_prob},
                ))
        day += timedelta(days=1)
    return out


def silver_bullet_overlays(
    view_start_s: int,
    view_end_s: int,
    agent_version: str,
) -> list[Overlay]:
    """Generate Silver Bullet window overlays for the view window."""
    from datetime import timedelta

    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}
    out: list[Overlay] = []
    dt_start = datetime.fromtimestamp(view_start_s, UTC)
    dt_end = datetime.fromtimestamp(view_end_s, UTC)
    day = dt_start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < dt_end:
        day_ny = day.astimezone(_NY)
        for sb_name, ((oh, om), (ch, cm)) in _SB_DEF.items():
            sb_open = day_ny.replace(hour=oh, minute=om, second=0, microsecond=0)
            sb_close = day_ny.replace(hour=ch, minute=cm, second=0, microsecond=0)
            ss = max(int(sb_open.timestamp()), view_start_s)
            se = min(int(sb_close.timestamp()), view_end_s)
            if se > ss:
                out.append(ZoneOverlay(
                    **common,
                    id=f"sb_{sb_name}:{int(sb_open.timestamp())}",
                    kind=f"silver_bullet_{sb_name}",
                    start_time=ss,
                    end_time=se,
                    top=1e12,
                    bottom=0.0,
                    confirmed_at=ss,
                    color=SB_COLOR,
                    label=f"SB {sb_name.upper()}",
                    meta={"window": sb_name},
                ))
        day += timedelta(days=1)
    return out
