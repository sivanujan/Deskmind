/**
 * Typed client for the read-only Deskmind API (uv run deskmind api -> 127.0.0.1:8010).
 * Every call is a GET; the API has no trading endpoints.
 */
import type { OverlayResponse } from "./overlays";

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";
export const WS_URL = API_URL.replace(/^http/, "ws");

export interface Candle {
  time: number; // UNIX seconds, candle open (UTC)
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  source?: "parquet" | "postgres" | "redis";
}

export interface CandlesResponse {
  symbol: string;
  timeframe: string;
  count: number;
  candles: Candle[];
}

export interface SymbolsResponse {
  symbols: string[];
  timeframes: string[];
}

export interface TimeframeStatus {
  timeframe: string;
  last_closed_open_time: number | null;
  buffer_size: number;
  stream_age_s: number | null;
  open_candle_age_s: number | null;
  behind_candles: number | null;
  stale: boolean;
}

export interface SymbolStatus {
  symbol: string;
  timeframes: TimeframeStatus[];
  mark_price: string | null;
  funding_rate: string | null;
  next_funding_time: number | null;
  mark_age_s: number | null;
  open_interest: string | null;
  open_interest_age_s: number | null;
  last_bar_close: number | null;
}

export interface LiveStatus {
  level: "green" | "amber" | "red";
  reasons: string[];
  running: boolean;
  state: string | null;
  ws_state: string | null;
  heartbeat_age_s: number | null;
  reconnects: number | null;
  clock_drift_ms: number | null;
  stale_streams: string[];
  symbols: SymbolStatus[];
}

export interface SwingSummary {
  price: number;
  label: string | null;
  pivot_time: number;
  pivot_time_iso?: string;
  confirmed_at: number;
  confirmed_at_iso?: string;
  broken_at?: number | null;
}

export interface TrendTfState {
  as_of: number | null;
  as_of_iso?: string;
  trend: "bullish" | "bearish" | "ranging";
  reason: string;
  last_swing_high: SwingSummary | null;
  last_swing_low: SwingSummary | null;
  last_event: { type: string; direction: string; level: number; time: number; time_iso?: string } | null;
  active_trendline: { side: string; touches: number; slope_per_hour: number; last_touch_iso?: string } | null;
  atr: number | null;
}

export interface TrendReport {
  agent: string;
  agent_version: string;
  symbol: string;
  as_of: string;
  as_of_ms: number;
  timeframes: Record<string, TrendTfState>;
}

export interface LiveMessage {
  type: "snapshot" | "partial" | "closed";
  symbol: string;
  timeframe: string;
  is_open: boolean;
  candle: Candle;
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(path, API_URL);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url.pathname}`);
  return (await res.json()) as T;
}

export const api = {
  symbols: () => get<SymbolsResponse>("/api/symbols"),
  candles: (symbol: string, tf: string, opts: { end?: number; start?: number; limit?: number } = {}) =>
    get<CandlesResponse>("/api/candles", { symbol, tf, ...opts }),
  liveStatus: () => get<LiveStatus>("/api/live/status"),
  overlays: (symbol: string, tf: string, opts: { start?: number; end?: number; asOf?: number; runId?: string } = {}) =>
    get<OverlayResponse>("/api/overlays", { symbol, tf, start: opts.start, end: opts.end, as_of: opts.asOf, run_id: opts.runId }),
  backtestRuns: () => get<Array<{ run_id: string; strategy_name: string; split: string; created_at?: string }>>("/api/backtest/runs"),
  trendReport: (symbol: string, asOf?: number) =>
    get<TrendReport>("/api/agents/trend/report", { symbol, as_of: asOf }),
  liveSocketUrl: (symbol: string, tf: string) =>
    `${WS_URL}/ws/live?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`,
};
