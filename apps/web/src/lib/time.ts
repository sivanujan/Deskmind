const utcFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const colomboFmt = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Colombo",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

/** "26/09/2026, 10:15 UTC" */
export function formatUtc(unixSeconds: number): string {
  return `${utcFmt.format(new Date(unixSeconds * 1000))} UTC`;
}

/** "26/09, 15:45 Colombo" */
export function formatColombo(unixSeconds: number): string {
  return `${colomboFmt.format(new Date(unixSeconds * 1000))} Colombo`;
}

export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 120) return `${Math.round(seconds)}s`;
  if (seconds < 7200) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export const TIMEFRAME_SECONDS: Record<string, number> = {
  "5m": 300,
  "15m": 900,
  "1h": 3600,
  "2h": 7200,
};
