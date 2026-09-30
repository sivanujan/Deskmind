import type { Candle } from "./api";
import { OVERLAY_SCHEMA_VERSION, type Overlay } from "./overlays";

/** Dev-only overlays that exercise every primitive (marker, line, zone) on the loaded candles.
 *  Never shown in production builds; real overlays come from /api/overlays (Step 4). */
export const DEV_SAMPLE_AGENT = "dev-sample";

export function devSampleOverlays(candles: Candle[]): Overlay[] {
  if (process.env.NEXT_PUBLIC_ENABLE_DEV_OVERLAYS !== "true" || candles.length < 40) return [];
  const recent = candles.slice(-120);
  const hi = recent.reduce((a, c) => (c.high > a.high ? c : a));
  const lo = recent.reduce((a, c) => (c.low < a.low ? c : a));
  const first = recent[0];
  const last = recent[recent.length - 1];
  const zoneFrom = recent[Math.floor(recent.length * 0.6)];
  const base = {
    source_agent: DEV_SAMPLE_AGENT,
    agent_version: "0.0.0-dev",
    schema_version: OVERLAY_SCHEMA_VERSION,
  };
  return [
    { ...base, id: "dev-high", kind: "swing_high", type: "marker", time: hi.time, position: "above",
      shape: "arrowDown", color: "#f23645", text: "sample high" },
    { ...base, id: "dev-low", kind: "swing_low", type: "marker", time: lo.time, position: "below",
      shape: "arrowUp", color: "#089981", text: "sample low" },
    { ...base, id: "dev-line", kind: "trendline", type: "line", start_time: first.time,
      start_price: first.low, end_time: last.time, end_price: last.high, style: "dashed",
      color: "#5b9cf6", label: "sample line" },
    { ...base, id: "dev-zone", kind: "zone", type: "zone", start_time: zoneFrom.time,
      end_time: last.time, top: Math.max(zoneFrom.high, last.high), bottom: Math.min(zoneFrom.low, last.low),
      color: "#f5c54226", label: "sample zone" },
  ];
}
