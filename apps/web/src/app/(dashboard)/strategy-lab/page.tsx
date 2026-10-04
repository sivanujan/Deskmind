"use client";

import { useEffect, useRef, useState } from "react";
import { api, API_URL } from "@/lib/api";

interface RuleDetail {
  value: number;
  threshold: number;
  passed: boolean;
  rule: string;
}

interface StrategyJob {
  job_id: string;
  strategy_id: string;
  strategy_name: string;
  strategy_version: string;
  trial_k: number;
  status: string;
  progress_pct: number;
  current_step: string;
  reject_reason: string | null;
  time_per_coin_s: number | null;
  estimated_seconds: number | null;
  elapsed_seconds: number;
  created_at: string;
  completed_at: string | null;
  verdict: string;
  rules_verdict: Record<string, RuleDetail>;
  funnel: {
    total_universe: number;
    eligible_coins: number;
    selected_coins: number;
    profitable_2025: number;
  };
  selected_symbols: string[];
  candidate_coins: Array<{
    symbol: string;
    label: string;
    train_positions: number;
    train_pf: number;
    train_mean_r: number;
    val_positions: number;
    val_pf: number;
    val_mean_r: number;
    val_net_profit: number;
  }>;
  not_tradeable: Array<{ symbol: string; reason: string }>;
  per_coin_results: Array<{
    symbol: string;
    is_eligible: boolean;
    is_selected: boolean;
    is_profitable_2025: boolean;
    train_positions: number;
    train_pf: number;
    train_mean_r: number;
    train_max_dd_pct: number;
    train_net_profit: number;
    val_positions: any;
    val_pf: any;
    val_mean_r: any;
    val_net_profit: any;
    val_max_dd_pct: any;
  }>;
  concentration_warning: {
    top_4_profit_share_pct: number;
    top_4_coins: string[];
    is_concentrated: boolean;
  };
  comparison_row: {
    name: string;
    coins_count: number;
    pooled_positions: number;
    pooled_trades: number;
    pooled_mean_r: number;
    pooled_profit_factor: number;
    pooled_net_profit: number;
  };
  test_2026_status: string;
}

export default function StrategyLabPage() {
  const [trialCountK, setTrialCountK] = useState<number>(0);
  const [recentJobs, setRecentJobs] = useState<any[]>([]);
  const [activeJob, setActiveJob] = useState<StrategyJob | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [showConfirm2026, setShowConfirm2026] = useState(false);
  const [test2026Msg, setTest2026Msg] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const loadStatus = async () => {
    try {
      const data = await api.strategyLabStatus();
      setTrialCountK(data.trial_count_k);
      setRecentJobs(data.jobs);
      if (!activeJob && data.jobs.length > 0) {
        // Load latest job
        const latest = await api.strategyLabJob(data.jobs[0].job_id);
        setActiveJob(latest);
      }
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  // Poll active job while running
  useEffect(() => {
    if (!activeJob) return;
    const isRunning = ["QUEUED", "RUNNING_CHECKS", "ESTIMATING", "TRAIN_SCAN", "FREEZING", "VALIDATION_SCAN"].includes(
      activeJob.status
    );
    if (!isRunning) return;

    const timer = setInterval(async () => {
      try {
        const updated = await api.strategyLabJob(activeJob.job_id);
        setActiveJob(updated);
        if (updated.status === "COMPLETED" || updated.status === "REJECTED" || updated.status === "FAILED") {
          loadStatus();
        }
      } catch (e) {
        console.error(e);
      }
    }, 2000);

    return () => clearInterval(timer);
  }, [activeJob?.job_id, activeJob?.status]);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setIsUploading(true);
    setUploadError(null);

    try {
      const res = await api.uploadStrategy(file);
      if (res.job_id) {
        const job = await api.strategyLabJob(res.job_id);
        setActiveJob(job);
      }
      loadStatus();
    } catch (err: any) {
      setUploadError(err.message || "Upload failed");
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleConfirm2026 = async () => {
    if (!activeJob) return;
    try {
      const res = await api.test2026(activeJob.job_id, true);
      setTest2026Msg(res.message);
      setShowConfirm2026(false);
    } catch (err: any) {
      setTest2026Msg(`Error: ${err.message}`);
    }
  };

  return (
    <div className="space-y-6">
      {/* Top Header */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 border-b border-border/40 pb-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Strategy Lab</h1>
          <p className="text-sm text-muted-foreground">
            Standardized evaluation framework: AST sandboxing, automated checks, TRAIN selection, and 2025 validation.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="bg-card border border-border/60 px-4 py-2 rounded-lg text-center shadow-sm">
            <span className="text-xs uppercase text-muted-foreground font-semibold">Trial Count (K)</span>
            <div className="text-xl font-mono font-bold text-primary">{trialCountK}</div>
          </div>
          <label className="cursor-pointer bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2.5 rounded-md font-medium text-sm transition inline-flex items-center gap-2">
            <span>{isUploading ? "Uploading..." : "Upload Strategy (.py)"}</span>
            <input
              ref={fileInputRef}
              type="file"
              accept=".py"
              onChange={handleFileUpload}
              disabled={isUploading}
              className="hidden"
            />
          </label>
        </div>
      </div>

      {uploadError && (
        <div className="bg-destructive/15 border border-destructive/30 text-destructive text-sm p-4 rounded-lg">
          <span className="font-semibold">Upload Rejected: </span>
          {uploadError}
        </div>
      )}

      {/* Active Job or Empty State */}
      {!activeJob ? (
        <div className="border border-dashed border-border rounded-xl p-12 text-center space-y-4">
          <div className="text-4xl">🧪</div>
          <h3 className="text-lg font-semibold">No Strategy Loaded</h3>
          <p className="text-sm text-muted-foreground max-w-md mx-auto">
            Upload a single converted Python strategy file conforming to the Strategy Lab template to run the Phase 2
            pipeline automatically.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {/* Job Overview Card */}
          <div className="bg-card border border-border/60 rounded-xl p-5 shadow-sm space-y-4">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-lg font-bold">{activeJob.strategy_name}</span>
                  <span className="text-xs bg-muted px-2 py-0.5 rounded font-mono">v{activeJob.strategy_version}</span>
                  <span className="text-xs bg-muted px-2 py-0.5 rounded font-mono">Job #{activeJob.job_id}</span>
                </div>
                <div className="text-xs text-muted-foreground mt-1">
                  Trial #{activeJob.trial_k} • Created at {activeJob.created_at}
                </div>
              </div>

              {/* Verdict / Status Badge */}
              <div className="flex items-center gap-3">
                {activeJob.verdict === "PASS" ? (
                  <span className="bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border border-emerald-500/30 px-3 py-1 rounded-md text-sm font-bold tracking-wider">
                    VERDICT: PASS
                  </span>
                ) : activeJob.verdict === "FAIL" ? (
                  <span className="bg-red-500/15 text-red-600 dark:text-red-400 border border-red-500/30 px-3 py-1 rounded-md text-sm font-bold tracking-wider">
                    VERDICT: FAIL
                  </span>
                ) : activeJob.status === "REJECTED" ? (
                  <span className="bg-destructive/15 text-destructive border border-destructive/30 px-3 py-1 rounded-md text-sm font-bold tracking-wider">
                    REJECTED
                  </span>
                ) : (
                  <span className="bg-amber-500/15 text-amber-600 dark:text-amber-400 border border-amber-500/30 px-3 py-1 rounded-md text-sm font-medium animate-pulse">
                    {activeJob.status} ({activeJob.progress_pct}%)
                  </span>
                )}

                {/* Downloads */}
                <div className="flex gap-2">
                  <a
                    href={`${API_URL}/api/strategy-lab/jobs/${activeJob.job_id}/download/csv`}
                    download
                    className="border border-border/80 hover:bg-muted px-3 py-1.5 rounded-md text-xs font-medium"
                  >
                    CSV
                  </a>
                  <a
                    href={`${API_URL}/api/strategy-lab/jobs/${activeJob.job_id}/download/json`}
                    download
                    className="border border-border/80 hover:bg-muted px-3 py-1.5 rounded-md text-xs font-medium"
                  >
                    JSON
                  </a>
                </div>
              </div>
            </div>

            {/* Progress Bar */}
            {activeJob.status !== "COMPLETED" && activeJob.status !== "REJECTED" && (
              <div className="space-y-1.5 pt-2">
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>{activeJob.current_step}</span>
                  <span>{activeJob.progress_pct}%</span>
                </div>
                <div className="w-full bg-muted rounded-full h-2 overflow-hidden">
                  <div
                    className="bg-primary h-2 transition-all duration-300 rounded-full"
                    style={{ width: `${activeJob.progress_pct}%` }}
                  />
                </div>
                {activeJob.estimated_seconds && (
                  <div className="text-[11px] text-muted-foreground flex gap-4 pt-1">
                    <span>Benchmark: {activeJob.time_per_coin_s}s / coin</span>
                    <span>Est. Total: ~{activeJob.estimated_seconds}s</span>
                    <span>Elapsed: {activeJob.elapsed_seconds}s</span>
                  </div>
                )}
              </div>
            )}

            {activeJob.reject_reason && (
              <div className="bg-destructive/15 border border-destructive/30 text-destructive text-sm p-3 rounded-lg">
                <span className="font-semibold">Rejection Detail: </span>
                {activeJob.reject_reason}
              </div>
            )}
          </div>

          {/* Validation Rule Breakdown */}
          {activeJob.rules_verdict && Object.keys(activeJob.rules_verdict).length > 0 && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {Object.entries(activeJob.rules_verdict).map(([k, rule]) => (
                <div
                  key={k}
                  className={`p-4 rounded-xl border ${
                    rule.passed
                      ? "bg-emerald-500/5 border-emerald-500/30 text-emerald-950 dark:text-emerald-100"
                      : "bg-red-500/5 border-red-500/30 text-red-950 dark:text-red-100"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs uppercase font-bold tracking-wider opacity-80">
                      {k.replace(/_/g, " ")}
                    </span>
                    <span
                      className={`text-xs px-2 py-0.5 rounded font-bold ${
                        rule.passed
                          ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
                          : "bg-red-500/20 text-red-700 dark:text-red-300"
                      }`}
                    >
                      {rule.passed ? "PASS" : "FAIL"}
                    </span>
                  </div>
                  <div className="mt-2 text-2xl font-bold font-mono">
                    {typeof rule.value === "number" ? rule.value : String(rule.value)}
                  </div>
                  <div className="text-xs opacity-70 mt-1">{rule.rule}</div>
                </div>
              ))}
            </div>
          )}

          {/* Funnel Metrics */}
          <div className="bg-card border border-border/60 rounded-xl p-5 shadow-sm">
            <h3 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground mb-4">
              Discovery & Evaluation Funnel
            </h3>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-center">
              <div className="bg-muted/40 p-4 rounded-lg">
                <span className="text-xs text-muted-foreground">1. Total Universe</span>
                <div className="text-2xl font-mono font-bold mt-1">{activeJob.funnel.total_universe}</div>
                <span className="text-[11px] text-muted-foreground">USDT-M Perpetual</span>
              </div>
              <div className="bg-muted/40 p-4 rounded-lg">
                <span className="text-xs text-muted-foreground">2. Eligible Coins</span>
                <div className="text-2xl font-mono font-bold mt-1 text-primary">{activeJob.funnel.eligible_coins}</div>
                <span className="text-[11px] text-muted-foreground">30d Quote Vol ≥ $20M</span>
              </div>
              <div className="bg-muted/40 p-4 rounded-lg">
                <span className="text-xs text-muted-foreground">3. Selected (TRAIN)</span>
                <div className="text-2xl font-mono font-bold mt-1 text-primary">{activeJob.funnel.selected_coins}</div>
                <span className="text-[11px] text-muted-foreground">Frozen by rules_v1</span>
              </div>
              <div className="bg-muted/40 p-4 rounded-lg">
                <span className="text-xs text-muted-foreground">4. Profitable in 2025</span>
                <div className="text-2xl font-mono font-bold mt-1 text-emerald-600 dark:text-emerald-400">
                  {activeJob.funnel.profitable_2025}
                </div>
                <span className="text-[11px] text-muted-foreground">
                  {activeJob.funnel.selected_coins > 0
                    ? `${Math.round((activeJob.funnel.profitable_2025 / activeJob.funnel.selected_coins) * 100)}% cohort breadth`
                    : "0%"}
                </span>
              </div>
            </div>
          </div>

          {/* Concentration Warning */}
          {activeJob.concentration_warning?.top_4_profit_share_pct > 0 && (
            <div
              className={`p-4 rounded-xl border flex items-start gap-3 ${
                activeJob.concentration_warning.is_concentrated
                  ? "bg-amber-500/10 border-amber-500/30 text-amber-950 dark:text-amber-200"
                  : "bg-muted/40 border-border text-foreground"
              }`}
            >
              <div className="text-xl">⚠️</div>
              <div>
                <div className="text-sm font-bold">
                  Profit Concentration Warning: {activeJob.concentration_warning.top_4_profit_share_pct}% from Top 4 Coins
                </div>
                <div className="text-xs opacity-80 mt-1">
                  Top 4 drivers in 2025: {activeJob.concentration_warning.top_4_coins.join(", ")}.
                  {activeJob.concentration_warning.is_concentrated &&
                    " Caution: High concentration indicates alpha is heavily dependent on a handful of outliers."}
                </div>
              </div>
            </div>
          )}

          {/* Candidate Coins Table */}
          {activeJob.candidate_coins?.length > 0 && (
            <div className="bg-card border border-border/60 rounded-xl overflow-hidden shadow-sm">
              <div className="px-5 py-4 border-b border-border/60 bg-muted/20 flex items-center justify-between">
                <div>
                  <h3 className="font-semibold text-sm">Candidate Coins ({activeJob.candidate_coins.length})</h3>
                  <p className="text-xs text-muted-foreground">Selected on TRAIN and profitable in 2025 validation.</p>
                </div>
                <span className="text-xs bg-amber-500/20 text-amber-700 dark:text-amber-300 font-semibold px-2.5 py-1 rounded">
                  needs 2026 test — not for live trading yet
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-muted/40 text-muted-foreground border-b border-border/60 uppercase">
                    <tr>
                      <th className="py-2.5 px-4">Symbol</th>
                      <th className="py-2.5 px-4">TRAIN Positions</th>
                      <th className="py-2.5 px-4">TRAIN PF</th>
                      <th className="py-2.5 px-4">TRAIN Mean R</th>
                      <th className="py-2.5 px-4">2025 Positions</th>
                      <th className="py-2.5 px-4">2025 PF</th>
                      <th className="py-2.5 px-4">2025 Mean R</th>
                      <th className="py-2.5 px-4">2025 Net PnL ($)</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40 font-mono">
                    {activeJob.candidate_coins.map((c) => (
                      <tr key={c.symbol} className="hover:bg-muted/30">
                        <td className="py-2.5 px-4 font-semibold text-primary font-sans">{c.symbol}</td>
                        <td className="py-2.5 px-4">{c.train_positions}</td>
                        <td className="py-2.5 px-4">{c.train_pf.toFixed(2)}</td>
                        <td className="py-2.5 px-4">{c.train_mean_r.toFixed(4)}</td>
                        <td className="py-2.5 px-4">{c.val_positions}</td>
                        <td className="py-2.5 px-4">{c.val_pf.toFixed(2)}</td>
                        <td className="py-2.5 px-4 text-emerald-600 dark:text-emerald-400">+{c.val_mean_r.toFixed(4)}</td>
                        <td className="py-2.5 px-4 font-bold text-emerald-600 dark:text-emerald-400">
                          +${c.val_net_profit.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Universe Comparison Row */}
          {activeJob.comparison_row && activeJob.comparison_row.coins_count > 0 && (
            <div className="bg-card border border-border/60 rounded-xl p-5 shadow-sm">
              <h3 className="text-sm font-semibold uppercase tracking-wider text-muted-foreground mb-3">
                Comparison: 2025 Full Universe Trend Beta
              </h3>
              <div className="overflow-x-auto">
                <table className="w-full text-xs text-left">
                  <thead className="bg-muted/40 text-muted-foreground border-b border-border/60">
                    <tr>
                      <th className="py-2 px-4">Cohort</th>
                      <th className="py-2 px-4">Coins Count</th>
                      <th className="py-2 px-4">Pooled Positions</th>
                      <th className="py-2 px-4">Pooled Trades</th>
                      <th className="py-2 px-4">Pooled Mean R</th>
                      <th className="py-2 px-4">Pooled PF</th>
                      <th className="py-2 px-4">Pooled Net PnL</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border/40 font-mono">
                    <tr className="bg-muted/10">
                      <td className="py-2.5 px-4 font-semibold font-sans">Selected Cohort (2025)</td>
                      <td className="py-2.5 px-4">{activeJob.funnel.selected_coins}</td>
                      <td className="py-2.5 px-4">{activeJob.rules_verdict?.pooled_mean_r ? activeJob.selected_symbols.length : "-"}</td>
                      <td className="py-2.5 px-4">-</td>
                      <td className="py-2.5 px-4 text-primary font-bold">
                        {activeJob.rules_verdict?.pooled_mean_r?.value}
                      </td>
                      <td className="py-2.5 px-4 font-bold">
                        {activeJob.rules_verdict?.pooled_profit_factor?.value}
                      </td>
                      <td className="py-2.5 px-4">-</td>
                    </tr>
                    <tr>
                      <td className="py-2.5 px-4 font-semibold font-sans">{activeJob.comparison_row.name}</td>
                      <td className="py-2.5 px-4">{activeJob.comparison_row.coins_count}</td>
                      <td className="py-2.5 px-4">{activeJob.comparison_row.pooled_positions}</td>
                      <td className="py-2.5 px-4">{activeJob.comparison_row.pooled_trades}</td>
                      <td className="py-2.5 px-4">{activeJob.comparison_row.pooled_mean_r}</td>
                      <td className="py-2.5 px-4">{activeJob.comparison_row.pooled_profit_factor}</td>
                      <td className="py-2.5 px-4">${activeJob.comparison_row.pooled_net_profit}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* 2026 Test Gate Action */}
          <div className="bg-card border border-border/60 rounded-xl p-5 shadow-sm flex flex-col md:flex-row items-start md:items-center justify-between gap-4">
            <div>
              <div className="flex items-center gap-2">
                <h3 className="font-semibold text-sm">2026 Out-Of-Sample Test Gate</h3>
                <span
                  className={`text-xs px-2 py-0.5 rounded font-mono font-semibold ${
                    activeJob.verdict === "PASS"
                      ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  {activeJob.verdict === "PASS" ? "READY" : "LOCKED"}
                </span>
              </div>
              <p className="text-xs text-muted-foreground mt-1 max-w-xl">
                2026 data is strictly locked. Single-use test gate can only be unlocked when the strategy achieves PASS
                verdict on 2025 validation.
              </p>
            </div>
            <div>
              <button
                disabled={activeJob.verdict !== "PASS"}
                onClick={() => setShowConfirm2026(true)}
                className="bg-primary text-primary-foreground disabled:opacity-40 disabled:cursor-not-allowed hover:bg-primary/90 px-4 py-2 rounded-md font-medium text-xs transition"
              >
                Unlock & Run 2026 Test
              </button>
            </div>
          </div>

          {test2026Msg && (
            <div className="bg-muted p-4 rounded-lg text-xs font-mono">{test2026Msg}</div>
          )}

          {/* Modal confirmation for 2026 test */}
          {showConfirm2026 && (
            <div className="fixed inset-0 bg-background/80 backdrop-blur-sm z-50 flex items-center justify-center p-4">
              <div className="bg-card border border-border p-6 rounded-xl max-w-md w-full space-y-4 shadow-xl">
                <h3 className="text-lg font-bold">Confirm 2026 Test Execution</h3>
                <p className="text-sm text-muted-foreground">
                  The 2026 test is a <strong>one-time out-of-sample test</strong>. Running it will permanently record
                  the result and exhaust the out-of-sample validation period for this strategy.
                </p>
                <div className="flex justify-end gap-3 pt-2">
                  <button
                    onClick={() => setShowConfirm2026(false)}
                    className="border border-border px-4 py-2 rounded-md text-sm hover:bg-muted"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={handleConfirm2026}
                    className="bg-primary text-primary-foreground px-4 py-2 rounded-md text-sm font-medium hover:bg-primary/90"
                  >
                    Confirm & Execute
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
