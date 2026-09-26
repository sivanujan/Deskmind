"use client";

import type { TrendReport, TrendTfState } from "@/lib/api";

const TREND_STYLE: Record<TrendTfState["trend"], string> = {
  bullish: "text-up",
  bearish: "text-down",
  ranging: "text-muted",
};

function fmt(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const d = v >= 1000 ? 1 : v >= 10 ? 2 : 4;
  return v.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

function short(iso?: string | null): string {
  return iso ? iso.replace("T", " ").replace(":00Z", "Z").slice(5) : "—";
}

/** The trend agent's report as of a moment, for every timeframe (each with its own as_of). */
export default function TrendPanel({
  report,
  timeframes,
  error,
}: {
  report: TrendReport | null;
  timeframes: string[];
  error: string | null;
}) {
  return (
    <div className="border-b border-border bg-surface/60 px-4 py-1.5 text-xs">
      <div className="mb-1 flex items-center gap-2 text-muted">
        <span className="font-semibold text-foreground">Trend agent</span>
        {report ? (
          <span>
            v{report.agent_version} · report as of {report.as_of.replace("T", " ")}
          </span>
        ) : (
          <span>{error ?? "no report"}</span>
        )}
      </div>
      {report && (
        <div className="grid grid-cols-[3rem_4.5rem_7.5rem_1fr_1fr_1fr_8rem] gap-x-3 gap-y-0.5 font-mono">
          <span className="text-muted">tf</span>
          <span className="text-muted">trend</span>
          <span className="text-muted">reason</span>
          <span className="text-muted">last event</span>
          <span className="text-muted">swing high</span>
          <span className="text-muted">swing low</span>
          <span className="text-muted">tf as of</span>
          {timeframes.map((tf) => {
            const s = report.timeframes[tf];
            if (!s) return null;
            const ev = s.last_event;
            return [
              <span key={`${tf}-tf`}>{tf}</span>,
              <span key={`${tf}-t`} className={TREND_STYLE[s.trend]}>{s.trend}</span>,
              <span key={`${tf}-r`} className="truncate" title={s.reason}>{s.reason}</span>,
              <span key={`${tf}-e`} className="truncate">
                {ev ? `${ev.type} ${ev.direction === "up" ? "↑" : "↓"} ${fmt(ev.level)} @ ${short(ev.time_iso)}` : "—"}
              </span>,
              <span key={`${tf}-h`} className="truncate">
                {s.last_swing_high ? `${s.last_swing_high.label ?? "H"} ${fmt(s.last_swing_high.price)}` : "—"}
              </span>,
              <span key={`${tf}-l`} className="truncate">
                {s.last_swing_low ? `${s.last_swing_low.label ?? "L"} ${fmt(s.last_swing_low.price)}` : "—"}
                {s.active_trendline ? ` · line ${s.active_trendline.touches}T` : ""}
              </span>,
              <span key={`${tf}-a`} className="text-muted">{short(s.as_of_iso)}</span>,
            ];
          })}
        </div>
      )}
    </div>
  );
}
