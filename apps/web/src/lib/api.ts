/**
 * Typed client for the Deskmind API. Every function calls an endpoint that exists in the backend
 * (tests/test_no_fake_ui.py checks this against the FastAPI routes).
 * Strictly PAPER and TESTNET only. LIVE TRADING IS FORBIDDEN.
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

export interface SymbolEntry {
  symbol: string;
  /** Universe lifecycle state; null = configured only (DATA_SYMBOLS). */
  state: string | null;
  /** Failure or status detail when blocked or not tradeable. */
  reason?: string | null;
  configured: boolean;
  /** History downloaded (Parquet on disk). */
  has_data: boolean;
}

export interface SymbolsResponse {
  symbols: string[];
  timeframes: string[];
  entries: SymbolEntry[];
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

// ----------------------------------------------------------------------------
// Dashboard types: each mirrors a real backend response. No field here may be filled in by the
// UI when the backend has no value — show an empty state instead (see tests/test_no_fake_ui.py).
// ----------------------------------------------------------------------------

export interface ServiceState {
  name: string;
  status: "stopped" | "starting" | "running" | "crashing" | "stopping";
  pid: number | null;
  uptime_s: number; // matches SupervisorManager.get_status()
  restart_count: number;
  last_exit_code: number | null;
  enabled: boolean;
}

export interface BreakerTransition {
  timestamp_ms: number;
  timestamp_iso: string;
  from_state: string;
  to_state: string;
  reason: string;
}

export interface BreakerFileState {
  halt_state: string;
  peak_equity: string;
  start_of_day_equity: string;
  drawdown_pct_of_peak: number;
  consecutive_losses: number;
  cooldown_until_ms: number;
  transitions: BreakerTransition[];
  file: string;
}

export interface SystemStatus {
  mode_badge: string;
  trading_mode: "paper" | "testnet";
  approval_mode: "auto" | "manual";
  live_trading_allowed: false;
  services: Record<string, ServiceState>;
  /** null = unknown (trade service not running and no breaker state file). */
  risk_state: string | null;
  risk_state_source: string | null;
  circuit_breaker_tripped: boolean | null;
  risk_details: BreakerFileState | null;
  trade_status_age_s: number | null;
  stats: Record<string, number> | null;
  open_positions: Array<Record<string, unknown>> | null;
  alerts: string[];
}

/** ApprovalRequest.to_dict() from execution/approval.py (decimals as strings). */
export interface ApprovalRequest {
  id: string;
  intent: {
    symbol: string;
    side: string;
    expected_entry: string;
    stop_loss: string;
    take_profit: string;
    strategy_name: string;
    strategy_version: string;
    setup_id: string;
  };
  decision: { approved: boolean; leverage: number; qty: string; notional: string; liquidation_price: string };
  account: { equity: string; available_balance: string };
  market: { mark_price: string; atr: string };
  status: string;
  created_at: number;
  expires_at: number;
  resolved_at: number | null;
  resolution_note: string | null;
}

export interface BacktestJob {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  created_at: number | string | null;
  started_at: number | string | null;
  finished_at: number | string | null;
  progress_pct: number;
  run_id: string | null;
  error: string | null;
  metrics: Record<string, unknown> | null;
  request: Record<string, unknown>;
}

/** A run record from reports/backtests/registry.json. */
export interface BacktestRun {
  run_id: string;
  strategy_name: string;
  strategy_version: string;
  split: string;
  start_date: string;
  end_date: string;
  created_at: string;
  status: string;
  symbols: string[];
  params: Record<string, unknown>;
  agent_versions: Record<string, unknown>;
  engine_version: string;
  git_commit: string | null;
  notes: string | null;
  metrics: Record<string, unknown>;
}

export interface BacktestMeta {
  splits: Record<string, { start: string; end: string; ui_allowed: boolean }>;
  strategies: string[];
  defaults: { warmup_days: number; initial_cash: string; symbols: string[] };
}

export interface JournalTrade {
  id: number;
  created_at: string;
  symbol: string;
  side: string;
  entry: number;
  sl: number;
  tp: number;
  size: number;
  leverage: number | null;
  status: string;
  pnl: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  trading_mode: string | null;
  client_order_id: string;
}

export interface AuditLogEntry {
  id?: number;
  created_at: string;
  action: string;
  user: string;
  details: Record<string, unknown>;
  ip_address: string | null;
  status: string;
}

export interface AgentRuntime {
  state: "running" | "stopped" | "never_run" | "unknown";
  reason: string | null;
  heartbeat_age_s?: number | null;
  last_report_as_of?: string | null;
  reports_since_start?: number | null;
  last_error?: string | null;
  last_error_at?: number | null;
  errors_24h?: number;
  last_report_stored_at?: string | null;
  last_report_stored_as_of?: string | null;
  reports_stored_24h?: number;
  reports_error?: string;
}

export interface AgentEntry {
  name: string;
  version: string;
  kind: string;
  spec: string;
  summary: string;
  spec_intro: string | null;
  params: Record<string, unknown>;
  runtime: AgentRuntime;
}

export interface AgentsResponse {
  agents: AgentEntry[];
  planned: Array<{ name: string; roadmap_step: number; summary: string }>;
}

export interface StrategyEntry {
  name: string;
  version: string;
  description: string;
  params: Record<string, unknown>;
  agent_params: Record<string, unknown>;
  uses_agents: string[];
  timeframes: string[];
  runs_total: number;
  runs_by_split: Record<string, number>;
  latest_by_split: Record<string, Record<string, unknown>>;
  validation_status: string;
}

export interface RiskResponse {
  config: Record<string, string | number | boolean>;
  /** Inline comments / section headers from risk/config.py, keyed by field name. */
  notes: Record<string, string>;
  groups: Record<string, string>;
  hard_caps: Record<string, string | number>;
  trading_mode: string;
  equity_by_mode: Record<string, { used_when: string; source: string; starting_equity: string | null }>;
  current_equity: {
    value: string | null;
    available: string | null;
    source: string;
    status_age_s: number | null;
    reason: string | null;
  };
  breaker: {
    state: string | null;
    source: string | null;
    kill_switch_halted: boolean | null;
    file: BreakerFileState | null;
    reason: string | null;
  };
}

export interface AccountResponse {
  read_only: true;
  used_for_sizing: false;
  key_configured: boolean;
  snapshot: string | null;
  reason: string | null;
  synced_at?: string;
  permissions?: Record<string, boolean>;
  futures_read?: boolean;
  futures_reason?: string | null;
  balances?: Array<{ asset: string; wallet_balance: string; unrealized_pnl: string; margin_balance: string }>;
  commission_rates?: Record<string, Record<string, unknown>>;
  brackets?: Record<string, Array<Record<string, unknown>>>;
  comparison?: Record<string, Array<Record<string, unknown>>>;
}

export interface ExternalTrade {
  source: "api" | "csv";
  market: "spot" | "futures";
  symbol: string;
  side: string;
  price: string;
  qty: string;
  quote_qty: string | null;
  fee: string | null;
  fee_asset: string | null;
  realized_pnl: string | null;
  /** UNIX milliseconds UTC */
  time_ms: number;
  order_id: string | null;
  trade_id: string;
}

export interface ExternalTradeStats {
  trades: number;
  per_symbol: Record<string, number>;
  fees_by_asset: Record<string, string>;
  pnl_fills: {
    winning: number;
    losing: number;
    zero_pnl: number;
    win_rate: number | null;
    realized_pnl_total: string;
    note: string;
  };
}


// ----------------------------------------------------------------------------
// CSRF & Session Management
// ----------------------------------------------------------------------------

let inMemoryCsrfToken = "";

export function getCsrfToken(): string {
  if (typeof window !== "undefined") {
    return inMemoryCsrfToken || localStorage.getItem("deskmind_csrf_token") || "";
  }
  return inMemoryCsrfToken;
}

export function setCsrfToken(token: string): void {
  inMemoryCsrfToken = token;
  if (typeof window !== "undefined") {
    localStorage.setItem("deskmind_csrf_token", token);
  }
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(path, API_URL);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined) url.searchParams.set(k, String(v));
  }
  const res = await fetch(url, {
    cache: "no-store",
    credentials: "include",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} for ${url.pathname}`);
  return (await res.json()) as T;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const url = new URL(path, API_URL);
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getCsrfToken();
  if (token) {
    headers["X-CSRF-Token"] = token;
  }
  const res = await fetch(url, {
    method: "POST",
    headers,
    credentials: "include",
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errorData.detail || `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

export interface UniverseSymbol {
  symbol: string;
  state: "watch_only" | "downloading" | "verifying" | "warming_up" | "tradeable" | "blocked" | "exited";
  rank: number | null;
  median_quote_volume: number | null;
  onboard_ms: number | null;
  member: boolean;
  data_verified: boolean;
  warmed_up: boolean;
  reason: string | null;
  entered_at_ms: number | null;
  exited_at_ms: number | null;
  state_changed_at_ms: number | null;
  history: Array<{ at_ms: number; from: string; to: string; reason: string | null }>;
}

export interface UniverseRunSummary {
  started_at: string;
  finished_at: string | null;
  run_date: string;
  entered: string[];
  exited: string[];
  new_watch_only: string[];
  downloads_done: string[];
  downloads_failed: Array<{ symbol: string; error: string }>;
  verify_failed: Array<{ symbol: string; detail: string }>;
  updated?: string[];
  update_failed?: Array<{ symbol: string; error: string }>;
  promoted?: string[];
  alerts: string[];
  members?: number;
  tradeable?: string[];
  by_state?: Record<string, number>;
  disk?: { used_pct: number; free_bytes: number; total_bytes: number; data_dir_bytes: number };
}

export interface UniverseResponse {
  config: Record<string, number>;
  trade_symbols: string[];
  symbols: UniverseSymbol[];
  runs: UniverseRunSummary[];
  reason: string | null;
}

export type TaskKind = "universe_run" | "account_sync";

export interface TaskState {
  kind: TaskKind;
  state: "running" | "done" | "failed";
  started_at: number;
  finished_at: number | null;
  user: string;
  result: Record<string, unknown> | null;
  error: string | null;
}

export const api = {
  // Public data & charts
  symbols: () => get<SymbolsResponse>("/api/symbols"),
  candles: (symbol: string, tf: string, opts: { end?: number; start?: number; limit?: number } = {}) =>
    get<CandlesResponse>("/api/candles", { symbol, tf, ...opts }),
  liveStatus: () => get<LiveStatus>("/api/live/status"),
  overlays: (symbol: string, tf: string, opts: { start?: number; end?: number; asOf?: number; runId?: string } = {}) =>
    get<OverlayResponse>("/api/overlays", { symbol, tf, start: opts.start, end: opts.end, as_of: opts.asOf, run_id: opts.runId }),
  trendReport: (symbol: string, asOf?: number) =>
    get<TrendReport>("/api/agents/trend/report", { symbol, as_of: asOf }),
  liveSocketUrl: (symbol: string, tf: string) =>
    `${WS_URL}/ws/live?symbol=${encodeURIComponent(symbol)}&tf=${encodeURIComponent(tf)}`,

  // Auth
  login: async (username: string, password: string) => {
    const data = await post<{ ok: boolean; username: string; csrf_token: string }>("/api/auth/login", {
      username,
      password,
    });
    if (data.csrf_token) {
      setCsrfToken(data.csrf_token);
    }
    return data;
  },
  logout: () => post<{ ok: boolean }>("/api/auth/logout"),
  me: () => get<{ username: string; authenticated: boolean; mode: string }>("/api/auth/me"),

  // System & service control
  systemStatus: () => get<SystemStatus>("/api/system/status"),
  serviceControl: (service: "live" | "trend" | "trade" | "universe", action: "start" | "stop" | "restart") =>
    post<{ service: string; action: string; success: boolean }>("/api/control/service", { service, action }),
  stopAllServices: (confirm: string) =>
    post<{ success: boolean; results: Record<string, boolean> }>("/api/control/service/stop-all", { confirm }),
  setMode: (trading_mode: "paper" | "testnet", approval_mode: "auto" | "manual") =>
    post<{ trading_mode: string; approval_mode: string }>("/api/control/mode", { trading_mode, approval_mode }),
  listApprovals: () => get<ApprovalRequest[]>("/api/control/pending"),
  decideApproval: (reqId: string, action: "approve" | "reject") =>
    post<{ req_id: string; status: string }>(`/api/control/${action}`, { req_id: reqId }),
  killSwitch: (confirm: string) => post<{ status: string; message: string }>("/api/control/kill", { confirm }),
  resetBreaker: (confirm: string) =>
    post<{ status: string; circuit_breaker: string }>("/api/control/breaker/reset", { confirm }),

  // Backtest Lab
  backtestMeta: () => get<BacktestMeta>("/api/dashboard/backtest-meta"),
  submitBacktest: (req: {
    strategy: string;
    split: "train" | "validation";
    symbols: string[];
    warmup_days?: number;
    initial_cash?: string;
  }) => post<{ job_id: string; status: string }>("/api/control/backtest/submit", req),
  cancelBacktest: (jobId: string) =>
    post<{ job_id: string; cancelled: boolean }>("/api/control/backtest/cancel", { job_id: jobId }),
  backtestJobs: () => get<BacktestJob[]>("/api/backtest/jobs"),
  backtestJobStatus: (jobId: string) => get<BacktestJob>(`/api/backtest/jobs/${encodeURIComponent(jobId)}`),
  backtestRuns: () => get<BacktestRun[]>("/api/backtest/runs"),
  backtestRunDetail: (runId: string) => get<BacktestRun>(`/api/backtest/runs/${encodeURIComponent(runId)}`),
  compareRuns: (runA: string, runB: string) =>
    get<{ run_a: BacktestRun; run_b: BacktestRun }>("/api/backtest/compare", { run_a: runA, run_b: runB }),
  backtestSocketUrl: (jobId: string) => `${WS_URL}/ws/backtest/${encodeURIComponent(jobId)}`,

  // Journal (Deskmind's own trades) & the user's imported Binance history
  journal: (params?: { symbol?: string; status?: string; limit?: number }) =>
    get<JournalTrade[]>("/api/journal/trades", params),
  externalTrades: (params?: { market?: string; symbol?: string; start?: number; end?: number; limit?: number }) =>
    get<{ totals_by_market: Record<string, number>; trades: ExternalTrade[] }>("/api/external-trades", params),
  externalTradeStats: (params?: { market?: string; symbol?: string; start?: number; end?: number }) =>
    get<ExternalTradeStats>("/api/external-trades/stats", params),
  uploadTradeCsv: async (file: File, market?: "spot" | "futures") => {
    const url = new URL("/api/external-trades/upload", API_URL);
    url.searchParams.set("filename", file.name);
    if (market) url.searchParams.set("market", market);
    const headers: Record<string, string> = { "Content-Type": "text/csv" };
    const token = getCsrfToken();
    if (token) headers["X-CSRF-Token"] = token;
    const res = await fetch(url, { method: "POST", headers, credentials: "include", body: await file.text() });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return (await res.json()) as { market: string; rows: number; inserted: number; duplicates: number };
  },

  // Truth endpoints (registry, strategy classes, RiskConfig, read-only mainnet snapshot)
  agents: () => get<AgentsResponse>("/api/dashboard/agents"),
  strategies: () => get<StrategyEntry[]>("/api/dashboard/strategies"),
  risk: () => get<RiskResponse>("/api/dashboard/risk"),
  account: () => get<AccountResponse>("/api/dashboard/account"),
  universe: () => get<UniverseResponse>("/api/universe"),

  // One-off jobs started from the dashboard (login + CSRF; audit-logged)
  runUniverseJob: () => post<TaskState>("/api/control/universe/run"),
  syncAccount: () => post<TaskState>("/api/control/account/sync"),
  // Audit
  auditLogs: (params?: { limit?: number; action?: string }) => get<AuditLogEntry[]>("/api/audit/logs", params),

  // Strategy Lab Phase B
  strategyLabStatus: () => get<{ trial_count_k: number; strategies_count: number; strategies: any[]; jobs: any[] }>("/api/strategy-lab/status"),
  strategyLabJobs: () => get<any[]>("/api/strategy-lab/jobs"),
  strategyLabJob: (jobId: string) => get<any>(`/api/strategy-lab/jobs/${jobId}`),
  uploadStrategy: async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const url = new URL("/api/strategy-lab/upload", API_URL);
    const res = await fetch(url, { method: "POST", body: formData, credentials: "include" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `${res.status} ${res.statusText}`);
    }
    return await res.json();
  },
  test2026: (jobId: string, confirm: boolean) =>
    post<{ status: string; message: string }>(`/api/strategy-lab/jobs/${jobId}/test-2026`, { confirm }),
};

