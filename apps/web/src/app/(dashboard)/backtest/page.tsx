"use client";

import { useEffect, useState, useRef } from "react";
import Link from "next/link";
import { api, type BacktestJob, type BacktestRunDetail } from "@/lib/api";

export default function BacktestLabPage() {
  const [strategy, setStrategy] = useState("trend_follow_v1");
  const [symbols, setSymbols] = useState("BTCUSDT,ETHUSDT");
  const [split, setSplit] = useState<"train" | "validation">("train");
  const [warmupBars, setWarmupBars] = useState("200");
  const [seeds, setSeeds] = useState("42");

  const [activeJob, setActiveJob] = useState<BacktestJob | null>(null);
  const [activeRun, setActiveRun] = useState<BacktestRunDetail | null>(null);
  const [registryRuns, setRegistryRuns] = useState<Array<{ run_id: string; strategy_name: string; split: string; created_at?: string }>>([]);

  const [compareA, setCompareA] = useState("");
  const [compareB, setCompareB] = useState("");
  const [compareResult, setCompareResult] = useState<{ run_a: BacktestRunDetail; run_b: BacktestRunDetail } | null>(null);

  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");
  const wsRef = useRef<WebSocket | null>(null);

  const loadRegistry = async () => {
    try {
      const runs = await api.backtestRuns();
      setRegistryRuns(runs);
      if (runs.length > 0 && !activeRun) {
        // Load first run by default if none active
        loadRunDetail(runs[0].run_id);
      }
    } catch {
      // Ignored
    }
  };

  const loadRunDetail = async (runId: string) => {
    try {
      const detail = await api.backtestRunDetail(runId);
      setActiveRun(detail);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to load run details");
    }
  };

  useEffect(() => {
    let mounted = true;
    void api.backtestRuns().then((runs) => {
      if (!mounted) return;
      setRegistryRuns(runs);
      if (runs.length > 0) {
        void api.backtestRunDetail(runs[0].run_id).then((detail) => {
          if (mounted) setActiveRun(detail);
        }).catch(() => {});
      }
    }).catch(() => {});
    return () => {
      mounted = false;
    };
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErrorMsg("");
    setIsSubmitting(true);

    const symbolList = symbols.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
    const seedList = seeds.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n));

    try {
      const job = await api.submitBacktest({
        strategy,
        split, // train or validation ONLY (hard safety rule)
        symbols: symbolList,
        warmup_bars: parseInt(warmupBars, 10) || 200,
        seeds: seedList.length > 0 ? seedList : [42],
      });

      setActiveJob(job);

      // Connect to WebSocket for streaming progress
      if (wsRef.current) wsRef.current.close();
      const ws = new WebSocket(api.backtestSocketUrl(job.job_id));
      wsRef.current = ws;

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          setActiveJob((prev) => (prev ? { ...prev, progress: data.progress, status: data.status } : null));
          if (data.status === "completed" && data.result_run_id) {
            loadRunDetail(data.result_run_id);
            loadRegistry();
            ws.close();
          }
        } catch {
          // JSON parse ignore
        }
      };

      ws.onerror = () => {
        // Fallback polling if WS closed
        const poll = setInterval(async () => {
          const status = await api.backtestJobStatus(job.job_id);
          setActiveJob(status);
          if (status.status === "completed" || status.status === "failed") {
            clearInterval(poll);
            if (status.result_run_id) loadRunDetail(status.result_run_id);
            loadRegistry();
          }
        }, 1000);
      };
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to launch backtest");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleCompare = async () => {
    if (!compareA || !compareB) return;
    try {
      const res = await api.compareRuns(compareA, compareB);
      setCompareResult(res);
    } catch (err: unknown) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to compare runs");
    }
  };

  const metrics = activeRun?.metrics;
  const isSmallSample = (metrics?.trade_count ?? 0) < 100;

  return (
    <div className="flex-1 space-y-6 p-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-bold text-foreground">Backtest Laboratory</h1>
          <p className="text-xs text-muted">
            Launch verified historical simulations on Train and Validation splits with rigorous cost modeling.
          </p>
        </div>

        <div className="rounded border border-accent/30 bg-accent/10 px-3 py-1 text-xs text-accent font-medium">
          Split Safety: TEST split locked (CLI only)
        </div>
      </div>

      {errorMsg && (
        <div className="rounded border border-down/40 bg-down/10 p-3 text-xs text-down">
          {errorMsg}
        </div>
      )}

      {/* Top Grid: Config & Job Status */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Backtest Launch Form */}
        <div className="rounded-lg border border-border bg-surface p-5 lg:col-span-2">
          <h2 className="text-sm font-semibold text-foreground border-b border-border pb-2">
            Configure New Simulation Run
          </h2>

          <form onSubmit={handleSubmit} className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div>
              <label className="block text-xs font-medium text-muted">Strategy</label>
              <select
                value={strategy}
                onChange={(e) => setStrategy(e.target.value)}
                className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:border-accent focus:outline-hidden"
              >
                <option value="trend_follow_v1">trend_follow_v1 (BOS + Trendline)</option>
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-muted">Data Split (UI Locked)</label>
              <select
                value={split}
                onChange={(e) => setSplit(e.target.value as "train" | "validation")}
                className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:border-accent focus:outline-hidden"
              >
                <option value="train">TRAIN (2021-01 to 2023-01)</option>
                <option value="validation">VALIDATION (2023-01 to 2024-01)</option>
                {/* Notice: 'test' split is strictly excluded from UI options */}
              </select>
            </div>

            <div>
              <label className="block text-xs font-medium text-muted">Symbols (comma-separated)</label>
              <input
                type="text"
                value={symbols}
                onChange={(e) => setSymbols(e.target.value)}
                className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:border-accent focus:outline-hidden"
                placeholder="BTCUSDT,ETHUSDT,SOLUSDT"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-muted">Warmup Bars</label>
              <input
                type="number"
                value={warmupBars}
                onChange={(e) => setWarmupBars(e.target.value)}
                className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:border-accent focus:outline-hidden"
              />
            </div>

            <div>
              <label className="block text-xs font-medium text-muted">RNG Seeds</label>
              <input
                type="text"
                value={seeds}
                onChange={(e) => setSeeds(e.target.value)}
                className="mt-1 w-full rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground focus:border-accent focus:outline-hidden"
                placeholder="42, 123"
              />
            </div>

            <div className="flex items-end">
              <button
                type="submit"
                disabled={isSubmitting}
                className="w-full rounded bg-accent py-2 text-xs font-semibold text-white hover:bg-accent/80 transition-colors disabled:opacity-50"
              >
                {isSubmitting ? "Submitting Job..." : "Queue Backtest Run"}
              </button>
            </div>
          </form>
        </div>

        {/* Live Job Progress / Status */}
        <div className="rounded-lg border border-border bg-surface p-5 flex flex-col justify-between">
          <div>
            <h2 className="text-sm font-semibold text-foreground border-b border-border pb-2">
              Queue & Job Status
            </h2>
            {activeJob ? (
              <div className="mt-4 space-y-3">
                <div className="flex justify-between text-xs">
                  <span className="text-muted">Job ID:</span>
                  <span className="font-mono text-foreground">{activeJob.job_id.slice(0, 14)}...</span>
                </div>
                <div className="flex justify-between text-xs">
                  <span className="text-muted">Status:</span>
                  <span className="font-bold uppercase text-accent">{activeJob.status}</span>
                </div>

                <div className="space-y-1">
                  <div className="flex justify-between text-xs text-muted">
                    <span>Progress:</span>
                    <span>{(activeJob.progress * 100).toFixed(1)}%</span>
                  </div>
                  <div className="h-2 w-full rounded-full bg-surface-2 overflow-hidden">
                    <div
                      className="h-full bg-accent transition-all duration-300"
                      style={{ width: `${Math.max(5, activeJob.progress * 100)}%` }}
                    />
                  </div>
                </div>

                {activeJob.status === "running" && (
                  <button
                    onClick={() => api.cancelBacktest(activeJob.job_id)}
                    className="mt-2 w-full rounded border border-down/40 bg-down/10 py-1 text-xs text-down hover:bg-down/20"
                  >
                    Cancel Execution
                  </button>
                )}
              </div>
            ) : (
              <div className="py-8 text-center text-xs text-muted">
                No active job running. Select or submit a run to monitor progress.
              </div>
            )}
          </div>

          <div className="mt-4 border-t border-border pt-3 text-[11px] text-muted">
            Concurrency limited to 1-2 workers to protect system stability.
          </div>
        </div>
      </div>

      {/* Results View */}
      {activeRun ? (
        <div className="space-y-6">
          {/* Small Sample Warning */}
          {isSmallSample && (
            <div className="flex items-center gap-3 rounded-lg border border-warn/40 bg-warn/10 p-4 text-xs text-warn">
              <span className="text-lg">⚠️</span>
              <div>
                <span className="font-bold">Small Sample Warning: </span>
                This simulation produced only {metrics?.trade_count ?? 0} trades (&lt; 100 trades threshold).
                Statistical metrics (Sharpe ratio, win rate) are subject to high variance and are not statistically significant.
              </div>
            </div>
          )}

          {/* Key Metrics Grid */}
          <div className="rounded-lg border border-border bg-surface p-5">
            <div className="flex flex-wrap items-center justify-between border-b border-border pb-3">
              <div>
                <h2 className="text-sm font-semibold text-foreground">
                  Run Results: <span className="font-mono text-accent">{activeRun.run_id}</span>
                </h2>
                <p className="text-xs text-muted">
                  Strategy: {activeRun.strategy_name} | Split: {activeRun.split.toUpperCase()}
                </p>
              </div>
              <Link
                href={`/chart?runId=${activeRun.run_id}`}
                className="rounded bg-accent/20 border border-accent/40 px-3 py-1 text-xs text-accent hover:bg-accent/30"
              >
                Inspect Trades on Chart &rarr;
              </Link>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
              <div className="rounded bg-surface-2 p-3 text-center">
                <span className="text-[11px] text-muted">Trade Count</span>
                <div className="mt-1 text-lg font-bold text-foreground">{metrics?.trade_count ?? 0}</div>
              </div>
              <div className="rounded bg-surface-2 p-3 text-center">
                <span className="text-[11px] text-muted">Sharpe Ratio</span>
                <div className="mt-1 text-lg font-bold text-foreground">{metrics?.sharpe_ratio?.toFixed(2) ?? "—"}</div>
              </div>
              <div className="rounded bg-surface-2 p-3 text-center">
                <span className="text-[11px] text-muted">Win Rate</span>
                <div className="mt-1 text-lg font-bold text-foreground">
                  {metrics?.win_rate ? `${(metrics.win_rate * 100).toFixed(1)}%` : "0.0%"}
                </div>
              </div>
              <div className="rounded bg-surface-2 p-3 text-center">
                <span className="text-[11px] text-muted">Max Drawdown</span>
                <div className="mt-1 text-lg font-bold text-down">
                  {metrics?.max_drawdown_pct ? `${(metrics.max_drawdown_pct * 100).toFixed(1)}%` : "0.0%"}
                </div>
              </div>
              <div className="rounded bg-surface-2 p-3 text-center">
                <span className="text-[11px] text-muted">Total Net PnL</span>
                <div
                  className={`mt-1 text-lg font-bold ${
                    (metrics?.total_pnl ?? 0) >= 0 ? "text-up" : "text-down"
                  }`}
                >
                  ${metrics?.total_pnl?.toFixed(2) ?? "0.00"}
                </div>
              </div>
              <div className="rounded bg-surface-2 p-3 text-center">
                <span className="text-[11px] text-muted">Profit Factor</span>
                <div className="mt-1 text-lg font-bold text-foreground">
                  {metrics?.profit_factor?.toFixed(2) ?? "—"}
                </div>
              </div>
            </div>

            {/* Cost Breakdown */}
            <div className="mt-4 rounded-md border border-border bg-background p-4">
              <span className="text-xs font-semibold text-foreground">Conservative Friction & Cost Audit</span>
              <div className="mt-2 grid grid-cols-2 gap-4 sm:grid-cols-4 text-xs">
                <div>
                  <span className="text-muted">Maker Fees (0.02%):</span>
                  <div className="font-mono text-foreground">${metrics?.maker_fees?.toFixed(2) ?? "0.00"}</div>
                </div>
                <div>
                  <span className="text-muted">Taker Fees (0.05%):</span>
                  <div className="font-mono text-foreground">${metrics?.taker_fees?.toFixed(2) ?? "0.00"}</div>
                </div>
                <div>
                  <span className="text-muted">Slippage Cost:</span>
                  <div className="font-mono text-foreground">${metrics?.slippage_cost?.toFixed(2) ?? "0.00"}</div>
                </div>
                <div>
                  <span className="text-muted">Funding Payments:</span>
                  <div className="font-mono text-foreground">${metrics?.funding_fees?.toFixed(2) ?? "0.00"}</div>
                </div>
              </div>
            </div>
          </div>

          {/* Rejections Table */}
          {activeRun.rejections && Object.keys(activeRun.rejections).length > 0 && (
            <div className="rounded-lg border border-border bg-surface p-5">
              <h3 className="text-xs font-semibold text-foreground uppercase tracking-wider text-muted">
                Risk Manager Intent Rejections Breakdown
              </h3>
              <div className="mt-3 overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-border text-muted">
                      <th className="pb-1.5 font-medium">Rejection Reason Code</th>
                      <th className="pb-1.5 font-medium text-right">Intent Count</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {Object.entries(activeRun.rejections).map(([reason, count]) => (
                      <tr key={reason}>
                        <td className="py-2 font-mono text-foreground">{reason}</td>
                        <td className="py-2 font-mono text-right text-warn">{count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="rounded-lg border border-border bg-surface p-8 text-center text-xs text-muted">
          Select a run from the registry below to display comprehensive performance analytics.
        </div>
      )}

      {/* Compare Runs Tool */}
      <div className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-sm font-semibold text-foreground border-b border-border pb-2">
          Compare Historical Runs
        </h2>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <select
            value={compareA}
            onChange={(e) => setCompareA(e.target.value)}
            className="rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground"
          >
            <option value="">Select Run A...</option>
            {registryRuns.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} ({r.split})
              </option>
            ))}
          </select>

          <span className="text-xs text-muted">vs</span>

          <select
            value={compareB}
            onChange={(e) => setCompareB(e.target.value)}
            className="rounded border border-border bg-background px-3 py-1.5 text-xs text-foreground"
          >
            <option value="">Select Run B...</option>
            {registryRuns.map((r) => (
              <option key={r.run_id} value={r.run_id}>
                {r.run_id} ({r.split})
              </option>
            ))}
          </select>

          <button
            onClick={handleCompare}
            disabled={!compareA || !compareB}
            className="rounded bg-accent/20 border border-accent/40 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/30 disabled:opacity-40"
          >
            Compare Side-by-Side
          </button>
        </div>

        {compareResult && (
          <div className="mt-4 grid grid-cols-2 gap-4 border-t border-border pt-4 text-xs">
            <div className="rounded bg-surface-2 p-3">
              <div className="font-mono font-bold text-accent">{compareResult.run_a.run_id}</div>
              <div className="mt-2 space-y-1 text-muted">
                <div>Trades: {compareResult.run_a.metrics?.trade_count}</div>
                <div>Sharpe: {compareResult.run_a.metrics?.sharpe_ratio?.toFixed(2)}</div>
                <div>Drawdown: {(compareResult.run_a.metrics?.max_drawdown_pct * 100).toFixed(1)}%</div>
                <div>Net PnL: ${compareResult.run_a.metrics?.total_pnl?.toFixed(2)}</div>
              </div>
            </div>

            <div className="rounded bg-surface-2 p-3">
              <div className="font-mono font-bold text-accent">{compareResult.run_b.run_id}</div>
              <div className="mt-2 space-y-1 text-muted">
                <div>Trades: {compareResult.run_b.metrics?.trade_count}</div>
                <div>Sharpe: {compareResult.run_b.metrics?.sharpe_ratio?.toFixed(2)}</div>
                <div>Drawdown: {(compareResult.run_b.metrics?.max_drawdown_pct * 100).toFixed(1)}%</div>
                <div>Net PnL: ${compareResult.run_b.metrics?.total_pnl?.toFixed(2)}</div>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Registry Table */}
      <div className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-sm font-semibold text-foreground border-b border-border pb-2">
          Run Registry Archive
        </h2>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="pb-2 font-medium">Run ID</th>
                <th className="pb-2 font-medium">Strategy</th>
                <th className="pb-2 font-medium">Split</th>
                <th className="pb-2 font-medium">Timestamp</th>
                <th className="pb-2 font-medium text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {registryRuns.map((r) => (
                <tr key={r.run_id} className="hover:bg-surface-2/40">
                  <td className="py-2.5 font-mono text-accent">{r.run_id}</td>
                  <td className="py-2.5 text-foreground">{r.strategy_name}</td>
                  <td className="py-2.5 uppercase font-medium">{r.split}</td>
                  <td className="py-2.5 text-muted">{r.created_at ?? "—"}</td>
                  <td className="py-2.5 text-right">
                    <button
                      onClick={() => loadRunDetail(r.run_id)}
                      className="rounded border border-border px-2 py-0.5 text-muted hover:text-foreground"
                    >
                      Load Metrics
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
