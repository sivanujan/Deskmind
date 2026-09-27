"use client";

import type { LiveStatus } from "@/lib/api";
import { formatAge } from "@/lib/time";

const STYLES = {
  green: "border-up/50 bg-up/15 text-up",
  amber: "border-warn/60 bg-warn/15 text-warn",
  red: "border-down/60 bg-down/15 text-down",
} as const;

const LABELS = { green: "Live", amber: "Degraded", red: "Offline" } as const;

export default function HealthBadge({ status, error }: { status: LiveStatus | null; error: string | null }) {
  const level = error || !status ? "red" : status.level;
  const title = error
    ? `API unreachable: ${error}`
    : status
      ? [
          `service: ${status.state ?? "—"} · websocket: ${status.ws_state ?? "—"}`,
          `heartbeat ${formatAge(status.heartbeat_age_s)} ago · reconnects ${status.reconnects ?? "unknown"}`,
          `clock drift ${status.clock_drift_ms ?? "—"} ms`,
          ...status.reasons,
        ].join("\n")
      : "loading…";
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 text-xs font-medium ${STYLES[level]}`}
      data-testid="health-badge"
      data-level={level}
    >
      <span className="h-2 w-2 rounded-full bg-current" />
      {error ? "API down" : LABELS[level]}
    </span>
  );
}
