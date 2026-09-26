"use client";

import type { Overlay } from "@/lib/overlays";

/** Hover details for the overlays on the hovered candle: rule, pivot_time, confirmed_at, level. */
export default function OverlayDetails({ overlays }: { overlays: Overlay[] }) {
  if (overlays.length === 0) return null;
  return (
    <div className="pointer-events-none absolute left-3 top-2 z-10 max-w-[28rem] space-y-1.5 rounded border border-border bg-surface/95 p-2 text-[11px] shadow-lg">
      {overlays.map((o) => (
        <div key={o.id}>
          <div className="font-semibold" style={{ color: o.color.slice(0, 7) }}>
            {o.type === "marker" ? o.text || o.kind : o.kind} <span className="text-muted">({o.source_agent})</span>
          </div>
          <dl className="grid grid-cols-[6.5rem_1fr] gap-x-2 font-mono text-muted">
            {Object.entries(o.meta ?? {})
              .filter(([, v]) => v !== null && v !== "")
              .map(([k, v]) => [
                <dt key={`${o.id}-${k}-k`}>{k}</dt>,
                <dd key={`${o.id}-${k}-v`} className="break-words text-foreground">
                  {typeof v === "number" ? v.toLocaleString("en-US", { maximumFractionDigits: 4 }) : String(v)}
                </dd>,
              ])}
          </dl>
        </div>
      ))}
    </div>
  );
}
