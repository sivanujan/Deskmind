"""Chart overlays for the Price Action agent (overlay contract v1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from deskmind.agents.price_action.config import AGENT_NAME
from deskmind.api.overlays import LineOverlay, MarkerOverlay, Overlay, ZoneOverlay

# S/R zone colours
SUPPORT_COLOR = "#22c55e"
RESISTANCE_COLOR = "#ef4444"
FLIPPED_COLOR = "#f59e0b"
RANGE_COLOR = "#64748b22"

# Strength → alpha: strong zones are more opaque
def _strength_alpha(strength: int) -> str:
    alpha = max(0x18, min(0x55, int(strength / 100 * 0x55)))
    return f"{alpha:02x}"


def _meta(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {
        "state": item.get("state"),
        "confirmed_at": datetime.fromtimestamp(
            item["confirmed_at"] / 1000, UTC
        ).strftime("%Y-%m-%dT%H:%M:%SZ") if item.get("confirmed_at") else None,
    }
    m.update({k: v for k, v in extra.items() if v is not None})
    return m


def _s(ms: int | None) -> int | None:
    return None if ms is None else ms // 1000


def to_overlays(items: list[dict[str, Any]], agent_version: str, view_end_ms: int) -> list[Overlay]:
    out: list[Overlay] = []
    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}

    for it in items:
        kind = it.get("kind", "")
        start_ms = it.get("created_time", 0)
        end_ms = view_end_ms

        # ---- S/R zones
        if kind in ("sr_support", "sr_resistance"):
            state = it.get("state", "active")
            side = it.get("side", kind.split("_")[1])
            top = it.get("top", 0.0)
            bottom = it.get("bottom", 0.0)
            strength = it.get("strength", 0)
            alpha = _strength_alpha(strength)
            if state == "flipped":
                col = FLIPPED_COLOR + alpha
            elif side == "support":
                col = SUPPORT_COLOR + alpha
            else:
                col = RESISTANCE_COLOR + alpha
            broken_or_end = it.get("broken_at") or view_end_ms
            end_s = min(broken_or_end, view_end_ms) // 1000
            out.append(ZoneOverlay(
                **common,
                id=it["id"],
                kind=kind,
                start_time=start_ms // 1000,
                end_time=end_s,
                top=top,
                bottom=bottom,
                confirmed_at=_s(it.get("confirmed_at")),
                color=col,
                label=f"{side[:3].upper()} {strength}",
                meta=_meta(it, strength=strength, side=side, mid=it.get("mid")),
            ))

        # ---- Breakouts
        elif kind == "breakout":
            direction = it.get("direction", "up")
            outcome = it.get("outcome")
            bo_time = it.get("breakout_time", start_ms)
            col = "#22c55e" if direction == "up" else "#ef4444"
            shape = "arrowUp" if direction == "up" else "arrowDown"
            position = "below" if direction == "up" else "above"
            out.append(MarkerOverlay(
                **common,
                id=it["id"],
                kind=kind,
                time=bo_time // 1000,
                position=position,
                shape=shape,
                confirmed_at=_s(it.get("confirmed_at")),
                color=col,
                text=f"BO {direction.upper()}" + (f" {outcome.upper()}" if outcome else ""),
                meta=_meta(it, zone_id=it.get("zone_id"), outcome=outcome),
            ))
            # Retest marker
            if it.get("retest_time"):
                rt = it["retest_time"]
                rt_col = "#22c55e" if outcome == "held" else "#ef4444" if outcome == "failed" else "#f59e0b"
                out.append(MarkerOverlay(
                    **common,
                    id=f"{it['id']}_retest",
                    kind="retest",
                    time=rt // 1000,
                    position="above" if direction == "up" else "below",
                    shape="circle",
                    confirmed_at=_s(rt),
                    color=rt_col,
                    text=f"RT {(outcome or '?').upper()}",
                    meta=_meta(it),
                ))

        # ---- Candle patterns
        elif kind.startswith("pattern_"):
            pattern = it.get("pattern", kind[len("pattern_"):])
            at_zone = it.get("at_zone")
            bullish = it.get("bullish", True)
            col = "#22c55e" if bullish else "#ef4444"
            position = "below" if bullish else "above"
            shape = "arrowUp" if bullish else "arrowDown"
            out.append(MarkerOverlay(
                **common,
                id=it["id"],
                kind=kind,
                time=start_ms // 1000,
                position=position,
                shape=shape,
                confirmed_at=_s(it.get("confirmed_at")),
                color=col,
                text=pattern.replace("_", " ").title()[:6],
                meta=_meta(
                    it,
                    pattern=pattern,
                    at_zone=at_zone,
                    context=it.get("context"),
                    size_atr=it.get("size_atr"),
                ),
            ))

        # ---- Ranges
        elif kind == "range":
            rh = it.get("range_high", 0.0)
            rl = it.get("range_low", 0.0)
            end_t = it.get("end_time") or view_end_ms
            out.append(ZoneOverlay(
                **common,
                id=it["id"],
                kind=kind,
                start_time=start_ms // 1000,
                end_time=min(end_t, view_end_ms) // 1000,
                top=rh,
                bottom=rl,
                confirmed_at=_s(it.get("confirmed_at")),
                color=RANGE_COLOR,
                label=f"RNG {it.get('bar_count', '')} bars",
                meta=_meta(it, breakout_dir=it.get("breakout_direction")),
            ))

    return out


def round_number_overlays(
    round_numbers: list[float],
    view_start_s: int,
    view_end_s: int,
    agent_version: str,
) -> list[Overlay]:
    """Horizontal dashed lines at round psychological levels."""
    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}
    out: list[Overlay] = []
    for price in round_numbers:
        out.append(LineOverlay(
            **common,
            id=f"round_{price:.10g}",
            kind="round_number",
            start_time=view_start_s,
            start_price=price,
            end_time=view_end_s,
            end_price=price,
            confirmed_at=view_start_s,
            style="dashed",
            color="#94a3b840",
            label=f"{price:.10g}",
            meta={"price": price, "type": "psychological_level"},
        ))
    return out
