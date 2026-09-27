/**
 * Typed client for the Deskmind Dashboard API (GETs & POSTs under /api/control/*).
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

// ----------------------------------------------------------------------------
// Dashboard Control Interfaces (Part 2)
// ----------------------------------------------------------------------------

export interface ServiceState {
  name: string;
  status: "stopped" | "starting" | "running" | "crashing" | "stopping";
  pid: number | null;
  uptime_seconds: number;
  restart_count: number;
  last_error: string | null;
}

export interface SystemStatus {
  trading_mode: "paper" | "testnet";
  approval_mode: "auto" | "manual";
  risk_state: "normal" | "half_risk" | "halt_and_close";
  circuit_breaker_tripped: boolean;
  services: Record<string, ServiceState>;
  open_positions_count: number;
  timestamp: number;
}

export interface PendingApproval {
  intent_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  strategy: string;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  quantity: number;
  risk_decision: {
    status: string;
    approved_fraction?: number;
    reason?: string;
  };
  expires_at: number;
  created_at: number;
}

export interface BacktestJob {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  request: {
    strategy: string;
    split: "train" | "validation";
    symbols: string[];
    start_date?: string;
    end_date?: string;
  };
  submitted_at: number;
  started_at?: number;
  completed_at?: number;
  result_run_id?: string;
  error?: string;
}

export interface BacktestRunDetail {
  run_id: string;
  strategy_name: string;
  split: string;
  created_at: string;
  metrics: {
    trade_count: number;
    win_rate: number;
    sharpe_ratio: number;
    cagr_pct: number;
    max_drawdown_pct: number;
    profit_factor: number;
    total_pnl: number;
    maker_fees: number;
    taker_fees: number;
    slippage_cost: number;
    funding_fees: number;
  };
  trades: Array<{
    trade_id: string;
    symbol: string;
    direction: string;
    entry_time: string;
    exit_time: string;
    entry_price: number;
    exit_price: number;
    size: number;
    pnl: number;
    pnl_pct: number;
    exit_reason: string;
  }>;
  equity_curve?: Array<{ time: number; equity: number; drawdown_pct: number }>;
  breaker_events?: Array<{ timestamp: string; state: string; reason: string }>;
  rejections?: Record<string, number>;
}

export interface TradeJournalEntry {
  trade_id: string;
  symbol: string;
  direction: "LONG" | "SHORT";
  status: "OPEN" | "CLOSED" | "CANCELLED";
  entry_price: number;
  exit_price?: number;
  quantity: number;
  pnl?: number;
  pnl_pct?: number;
  entry_time: string;
  exit_time?: string;
  strategy: string;
  fees: number;
  funding: number;
  slippage: number;
  risk_decision?: {
    status: string;
    reason?: string;
    circuit_breaker_state?: string;
  };
  orders?: Array<{ order_id: string; type: string; price: number; status: string }>;
  fills?: Array<{ fill_id: string; price: number; qty: number; fee: number }>;
}

export interface AuditLogEntry {
  id: number;
  timestamp: string;
  user: string;
  action: string;
  status: string;
  ip_address: string;
  details: Record<string, unknown>;
}

export interface AgentStatus {
  name: string;
  version: string;
  status: "online" | "idle" | "error" | "recovering";
  last_run: string;
  latency_ms: number;
  errors_24h: number;
  last_report_summary?: string;
}

export interface StrategyInfo {
  id: string;
  name: string;
  description: string;
  timeframe: string;
  edge_status: "verified" | "no_edge" | "untested";
  parameters: Record<string, unknown>;
  author: string;
}

export interface AccountInfo {
  source: string;
  date: string;
  balances: Array<{ asset: string; wallet_balance: number; unrealized_pnl: number; margin_balance: number }>;
  margin_ratio_pct: number;
  vip0_fee_rates: { maker: number; taker: number };
  account_fee_rates?: { maker: number; taker: number };
  restrictions: {
    can_trade: boolean;
    can_future_trade: boolean;
    can_withdraw: boolean;
    can_deposit: boolean;
  };
  mmr_brackets: Array<{ tier: number; max_notional: number; mmr_rate_pct: number }>;
}

export interface RiskStateInfo {
  state: "normal" | "half_risk" | "halt_and_close";
  current_drawdown_pct: number;
  peak_equity: number;
  current_equity: number;
  thresholds: {
    half_risk_trigger: number;
    half_risk_hysteresis: number;
    halt_trigger: number;
    halt_hysteresis: number;
    halt_min_days: number;
  };
  limits: {
    max_position_notional_usdt: number;
    max_leverage: number;
    daily_loss_limit_pct: number;
    max_open_trades: number;
    atr_stop_multiplier: number;
  };
  breaker_history: Array<{
    timestamp: string;
    previous_state: string;
    new_state: string;
    reason: string;
  }>;
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

  // System & Service Control
  systemStatus: () => get<SystemStatus>("/api/system/status"),
  serviceControl: (service: "live" | "trend" | "trade", action: "start" | "stop" | "restart") =>
    post<{ status: string; service: string }>("/api/control/service", { service, action }),
  stopAllServices: (confirm: string) =>
    post<{ status: string; stopped: Record<string, boolean> }>("/api/control/service/stop-all", { confirm }),
  setMode: (trading_mode: "paper" | "testnet", approval_mode: "auto" | "manual") =>
    post<{ status: string; trading_mode: string; approval_mode: string }>("/api/control/mode", {
      trading_mode,
      approval_mode,
    }),
  listApprovals: () => get<PendingApproval[]>("/api/control/approvals"),
  decideApproval: (intentId: string, action: "approve" | "reject") =>
    post<{ status: string; intent_id: string }>(`/api/control/approvals/${intentId}`, { action }),
  killSwitch: (confirm: string) =>
    post<{ status: string; message: string }>("/api/control/kill", { confirm }),
  resetBreaker: (confirm: string) =>
    post<{ status: string; message: string }>("/api/control/breaker/reset", { confirm }),

  // Backtest Lab
  submitBacktest: (req: {
    strategy: string;
    split: "train" | "validation";
    symbols: string[];
    start_date?: string;
    end_date?: string;
    warmup_bars?: number;
    seeds?: number[];
  }) => post<BacktestJob>("/api/control/backtest/submit", req),
  cancelBacktest: (jobId: string) => post<{ status: string }>(`/api/control/backtest/${jobId}/cancel`),
  backtestJobs: () => get<BacktestJob[]>("/api/control/backtest/jobs"),
  backtestJobStatus: (jobId: string) => get<BacktestJob>(`/api/control/backtest/jobs/${jobId}`),
  backtestRuns: () => get<Array<{ run_id: string; strategy_name: string; split: string; created_at?: string }>>("/api/control/backtest/registry"),
  backtestRunDetail: (runId: string) => get<BacktestRunDetail>(`/api/control/backtest/runs/${runId}`),
  compareRuns: (runA: string, runB: string) =>
    get<{ run_a: BacktestRunDetail; run_b: BacktestRunDetail }>("/api/control/backtest/compare", {
      run_a: runA,
      run_b: runB,
    }),
  backtestSocketUrl: (jobId: string) => `${WS_URL}/ws/backtest/${encodeURIComponent(jobId)}`,

  // Journal & Trades
  journal: (params?: { symbol?: string; status?: string; limit?: number; offset?: number }) =>
    get<TradeJournalEntry[]>("/api/control/journal", params),
  journalDetail: (tradeId: string) => get<TradeJournalEntry>(`/api/control/journal/${tradeId}`),

  // Risk
  riskState: () => get<RiskStateInfo>("/api/control/risk/state"),

  // Agents
  agents: () => get<AgentStatus[]>("/api/control/agents"),

  // Strategies
  strategies: () => get<StrategyInfo[]>("/api/control/strategies"),

  // Account (Part 1 read-only)
  accountInfo: () => get<AccountInfo>("/api/control/account/info"),

  // Audit
  auditLogs: (params?: { limit?: number; offset?: number; action?: string }) =>
    get<AuditLogEntry[]>("/api/control/audit/logs", params),
};
