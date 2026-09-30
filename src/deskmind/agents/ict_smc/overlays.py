"""ICT/SMC items -> chart overlays (overlay contract v1). Items must already be as-of views."""

from __future__ import annotations

from typing import Any

from deskmind.agents.ict_smc.agent import iso
from deskmind.agents.ict_smc.config import AGENT_NAME
from deskmind.api.overlays import LineOverlay, MarkerOverlay, Overlay, ZoneOverlay


def _s(ms: int | None) -> int | None:
    return None if ms is None else ms // 1000


def _meta(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    created = item.get("created_time", item.get("pivot_time", 0))
    meta: dict[str, Any] = {
        "rule": item.get("rule"),
        "created_time": iso(created),
        "confirmed_at": iso(item.get("confirmed_at")),
        "state": item.get("state"),
    }
    meta.update({k: v for k, v in extra.items() if v is not None})
    return meta


def to_overlays(items: list[dict[str, Any]], agent_version: str, view_end_ms: int) -> list[Overlay]:
    """Convert ICT/SMC items into chart overlays."""
    out: list[Overlay] = []
    common = {"source_agent": AGENT_NAME, "agent_version": agent_version}

    for it in items:
        kind = it.get("kind", "")

        # ---------------------------------------------------- BSL / SSL Pools
        if kind in ("bsl", "ssl"):
            start_ms = it["created_time"]
            ended = it.get("taken_at") or it.get("expired_at")
            end_ms = min(ended or view_end_ms, view_end_ms)
            if end_ms <= start_ms:
                end_ms = start_ms + 60_000

            is_bsl = it["side"] == "high"
            color = "#38bdf8" if is_bsl else "#ec4899"
            if it.get("state") == "taken":
                color += "40"  # dimmed
            elif it.get("state") == "swept":
                color += "80"

            label = f"{'BSL' if is_bsl else 'SSL'} ({it.get('pool_type', '').upper()})"
            out.append(
                LineOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    start_time=start_ms // 1000,
                    start_price=it["level"],
                    end_time=end_ms // 1000,
                    end_price=it["level"],
                    confirmed_at=_s(it["confirmed_at"]),
                    style="dashed",
                    color=color,
                    label=label,
                    meta=_meta(
                        it,
                        level=it["level"],
                        pool_type=it.get("pool_type"),
                        swept_at=iso(it.get("swept_at")),
                        swept_by=it.get("swept_by"),
                        taken_at=iso(it.get("taken_at")),
                    ),
                )
            )

            # Marked sweep
            if it.get("swept_at"):
                swept_s = it["swept_at"] // 1000
                out.append(
                    MarkerOverlay(
                        **common,
                        id=f"{it['id']}:swept",
                        kind="pool_sweep",
                        time=swept_s,
                        confirmed_at=_s(it["swept_at"]),
                        position="above" if is_bsl else "below",
                        shape="square",
                        color="#b388ff",
                        text="swept",
                        meta=_meta(it, level=it["level"], swept_at=iso(it["swept_at"])),
                    )
                )

        # ---------------------------------------------------- Fair Value Gaps
        elif kind.startswith("fvg"):
            start_ms = it["created_time"]
            ended = it.get("inverted_at") or it.get("filled_at") or it.get("expired_at")
            end_ms = min(ended or view_end_ms, view_end_ms)
            if end_ms <= start_ms:
                end_ms = start_ms + 60_000

            is_bullish = it["direction"] == "bullish"
            state = it.get("state", "fresh")
            if state == "inversion":
                color = "#f9731644"  # orange for inversion
                label = f"IFVG ({it['direction']})"
            elif is_bullish:
                color = "#22c55e33" if state != "filled" else "#22c55e14"
                label = f"Bullish FVG ({state})"
            else:
                color = "#ef444433" if state != "filled" else "#ef444414"
                label = f"Bearish FVG ({state})"

            out.append(
                ZoneOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    start_time=start_ms // 1000,
                    end_time=end_ms // 1000,
                    top=it["top"],
                    bottom=it["bottom"],
                    confirmed_at=_s(it["confirmed_at"]),
                    color=color,
                    label=label,
                    meta=_meta(
                        it,
                        direction=it["direction"],
                        top=it["top"],
                        bottom=it["bottom"],
                        ce=it.get("ce"),
                        filled_at=iso(it.get("filled_at")),
                        inverted_at=iso(it.get("inverted_at")),
                    ),
                )
            )

        # ---------------------------------------------------- Order Blocks
        elif kind.startswith("ob"):
            start_ms = it["created_time"]
            ended = it.get("invalidated_at") or it.get("expired_at")
            end_ms = min(ended or view_end_ms, view_end_ms)
            if end_ms <= start_ms:
                end_ms = start_ms + 60_000

            is_bullish = it["direction"] == "bullish"
            state = it.get("state", "fresh")
            if is_bullish:
                color = "#3b82f633" if state not in ("mitigated", "invalidated") else "#3b82f618"
                label = f"Bullish OB ({state})"
            else:
                color = "#f9731633" if state not in ("mitigated", "invalidated") else "#f9731618"
                label = f"Bearish OB ({state})"

            out.append(
                ZoneOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    start_time=start_ms // 1000,
                    end_time=end_ms // 1000,
                    top=it["top"],
                    bottom=it["bottom"],
                    confirmed_at=_s(it["confirmed_at"]),
                    color=color,
                    label=label,
                    meta=_meta(
                        it,
                        direction=it["direction"],
                        top=it["top"],
                        bottom=it["bottom"],
                        mt=it.get("mt"),
                        from_bos=it.get("from_bos_id"),
                        mitigated_at=iso(it.get("mitigated_at")),
                        invalidated_at=iso(it.get("invalidated_at")),
                    ),
                )
            )

        # ---------------------------------------------------- Breaker / Mitigation Blocks
        elif kind.startswith("breaker") or kind.startswith("mitigation"):
            start_ms = it["created_time"]
            ended = it.get("invalidated_at") or it.get("expired_at")
            end_ms = min(ended or view_end_ms, view_end_ms)
            if end_ms <= start_ms:
                end_ms = start_ms + 60_000

            is_mitigation = it.get("is_mitigation", False)
            title = "Mitigation Block" if is_mitigation else "Breaker Block"
            state = it.get("state", "active")
            color = "#a855f733" if state == "active" else "#a855f718"
            out.append(
                ZoneOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    start_time=start_ms // 1000,
                    end_time=end_ms // 1000,
                    top=it["top"],
                    bottom=it["bottom"],
                    confirmed_at=_s(it["confirmed_at"]),
                    color=color,
                    label=f"{title} ({it['direction']})",
                    meta=_meta(
                        it,
                        direction=it["direction"],
                        is_mitigation=is_mitigation,
                        top=it["top"],
                        bottom=it["bottom"],
                        source_ob=it.get("source_ob_id"),
                        sweep=it.get("sweep_id"),
                    ),
                )
            )

        # ---------------------------------------------------- Liquidity Voids
        elif kind == "liquidity_void":
            start_ms = it["created_time"]
            ended = it.get("filled_at") or it.get("expired_at")
            end_ms = min(ended or view_end_ms, view_end_ms)
            if end_ms <= start_ms:
                end_ms = start_ms + 60_000

            out.append(
                ZoneOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    start_time=start_ms // 1000,
                    end_time=end_ms // 1000,
                    top=it["top"],
                    bottom=it["bottom"],
                    confirmed_at=_s(it["confirmed_at"]),
                    color="#eab30822",
                    label=f"Void ({it.get('state')})",
                    meta=_meta(
                        it, direction=it.get("direction"), top=it["top"], bottom=it["bottom"]
                    ),
                )
            )

        # ---------------------------------------------------- SMT Divergence
        elif kind.startswith("smt"):
            out.append(
                MarkerOverlay(
                    **common,
                    id=it["id"],
                    kind=kind,
                    time=it["created_time"] // 1000,
                    confirmed_at=_s(it["confirmed_at"]),
                    position="above" if "bearish" in kind else "below",
                    shape="circle",
                    color="#ec4899",
                    text="SMT",
                    meta=_meta(
                        it,
                        pair=str(it.get("pair")),
                        divergence=it.get("divergence_type"),
                        note=it.get("note"),
                    ),
                )
            )

    return out
