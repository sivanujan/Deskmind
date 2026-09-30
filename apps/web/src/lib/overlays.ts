/**
 * Overlay contract, schema version 1 — TypeScript mirror of
 * src/deskmind/api/overlays.py (JSON Schema: docs/overlay.schema.json).
 * Keep both sides in sync; bump OVERLAY_SCHEMA_VERSION on any breaking change.
 *
 * Times are UNIX seconds (UTC). Prices are display floats.
 */
export const OVERLAY_SCHEMA_VERSION = 1 as const;

export type HexColor = string; // #rrggbb or #rrggbbaa

interface OverlayBase {
  id: string;
  /** Semantic kind, e.g. "swing_high", "bos", "choch", "trendline", "order_block". */
  kind: string;
  /** Agent that produced it, e.g. "trend". */
  source_agent: string;
  agent_version: string;
  schema_version: typeof OVERLAY_SCHEMA_VERSION;
  /** UNIX seconds when it became knowable; replay shows only confirmed_at <= T. */
  confirmed_at?: number | null;
  /** UI display flag: true when drawn faded in history mode. */
  isHistorical?: boolean;
  /** Details shown on hover (rule, pivot_time, confirmed_at, level, ...). */
  meta?: Record<string, string | number | boolean | null>;
}

export interface MarkerOverlay extends OverlayBase {
  type: "marker";
  time: number;
  position: "above" | "below";
  shape: "circle" | "square" | "arrowUp" | "arrowDown";
  color: HexColor;
  text: string;
}

export interface LineOverlay extends OverlayBase {
  type: "line";
  start_time: number;
  start_price: number;
  end_time: number;
  end_price: number;
  style: "solid" | "dashed" | "dotted";
  color: HexColor;
  label: string;
}

export interface ZoneOverlay extends OverlayBase {
  type: "zone";
  start_time: number;
  end_time: number;
  top: number;
  bottom: number;
  color: HexColor;
  label: string;
}

export type Overlay = MarkerOverlay | LineOverlay | ZoneOverlay;

export interface OverlayResponse {
  symbol: string;
  timeframe: string;
  schema_version: typeof OVERLAY_SCHEMA_VERSION;
  overlays: Overlay[];
}
