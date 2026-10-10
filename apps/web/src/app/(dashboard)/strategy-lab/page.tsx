"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, API_URL, type StrategyUpload } from "@/lib/api";
import { Badge, Card, Empty, PageHeader, Stat, useApi, val } from "@/components/ui";

const ALL_TIMEFRAMES = ["5m", "15m", "30m", "1h", "2h", "4h"];
const ORDERED_TIMEFRAMES = ["4h", "2h", "1h", "30m", "15m", "5m"];
const RUNNING_STATUSES = ["RUNNING_CHECKS", "ESTIMATING", "TRAIN_SCAN", "FREEZING", "VALIDATION_SCAN"];
const ACTIVE_STATUSES = ["QUEUED", ...RUNNING_STATUSES];

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
  timeframe?: string;
  status: string;
  progress_pct: number;
  current_step: string;
  current_coin?: string;
  coins_done?: number;
  coins_total?: number;
  scan_phase?: string;
  updated_at?: string;
  last_update_ts?: number;
  last_update_age_s?: number;
  is_stalled?: boolean;
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
  // The per-coin lists below are only in GET /jobs/{id}; the polled /status leaves them out.
  selected_symbols?: string[];
  selected_count?: number;
  candidate_coins?: Array<{
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
  not_tradeable?: Array<{ symbol: string; reason: string }>;
  per_coin_results?: Array<{
    symbol: string;
    is_eligible: boolean;
    is_selected: boolean;
    is_profitable_2025: boolean;
    train_positions: number;
    train_pf: number;
    train_mean_r: number;
    train_max_dd_pct: number;
    train_net_profit: number;
    val_positions: number | null;
    val_pf: number | null;
    val_mean_r: number | null;
    val_net_profit: number | null;
    val_max_dd_pct: number | null;
  }>;
  concentration_warning: {
    top_4_profit_share_pct: number;
    top_4_coins: string[];
    is_concentrated: boolean;
  };
  comparison_row: ComparisonRow;
  test_2026_status: string;
  eval_mode?: "standard" | "walk_forward";
  walkforward_rows?: Array<{
    quarter_id: string;
    quarter: string;
    train_start: string;
    train_end: string;
    test_start: string;
    test_end: string;
    coins_selected: number;
    coins: string[];
    positions: number;
    mean_r: number;
    profit_factor: number;
    profit: number;
    gross_profit?: number;
    gross_loss?: number;
    is_profitable: boolean;
  }>;
  walkforward_combined?: {
    quarter: string;
    quarter_id: string;
    coins_selected: number;
    positions: number;
    mean_r: number;
    profit_factor: number;
    profit: number;
    gross_profit?: number;
    gross_loss?: number;
    profitable_quarters: string;
    verdict: string;
  };
}

interface ComparisonRow {
    tf?: string;
    eligible_coins?: number;
    train_positions?: number;
    train_pooled_mean_r?: number;
    train_pooled_pf?: number;
    avg_cost_in_r?: number;
    selected?: number;
    val_positions?: number;
    val_pooled_mean_r?: number;
    val_pooled_pf?: number;
    pct_selected_profitable_2025?: number;
    verdict?: string;
}

type Tone = "up" | "down" | "accent" | "muted" | "warn";

/** One plain-English status per job. A verdict FAIL (no edge) is not the same as a run that crashed. */
function jobStatus(job: StrategyJob, nowTs: number): { label: string; tone: Tone; stalled: boolean; ageS: number } {
  const ageS = job.last_update_ts ? nowTs - job.last_update_ts : job.last_update_age_s ?? 0;
  const stalled = job.is_stalled || (RUNNING_STATUSES.includes(job.status) && ageS > 120);
  if (stalled) return { label: "Stalled", tone: "down", stalled, ageS };
  if (job.status === "COMPLETED") {
    return job.verdict === "PASS"
      ? { label: "Pass", tone: "up", stalled, ageS }
      : { label: "Fail · no edge", tone: "down", stalled, ageS };
  }
  if (job.status === "QUEUED") return { label: "Queued", tone: "muted", stalled, ageS };
  if (job.status === "CANCELLED") return { label: "Cancelled", tone: "muted", stalled, ageS };
  if (job.status === "FAILED") return { label: "Run error", tone: "warn", stalled, ageS };
  if (job.status === "REJECTED") return { label: "Rejected", tone: "warn", stalled, ageS };
  return { label: `Running ${job.progress_pct}%`, tone: "accent", stalled, ageS };
}

function signed(v: unknown, digits = 4): ReactNode {
  if (typeof v !== "number") return val(v);
  return v > 0 ? `+${v.toFixed(digits)}` : v.toFixed(digits);
}

function toneOf(v: unknown, pivot: number): string {
  if (typeof v !== "number") return "";
  return v > pivot ? "text-up" : v < pivot ? "text-down" : "";
}

function fmtDuration(s: number | null | undefined): string {
  if (!s || s <= 0) return "—";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
}

const COMPARISON_COLUMNS: Array<{ key: string; label: string; help: string; group: "" | "train" | "val" }> = [
  { key: "tf", label: "Timeframe", help: "Candle timeframe this run used.", group: "" },
  { key: "eligible_coins", label: "Coins", help: "Coins in the universe with enough liquidity and data to test.", group: "train" },
  { key: "train_positions", label: "Trades", help: "Positions taken in 2023–2024 across all eligible coins.", group: "train" },
  { key: "train_pooled_mean_r", label: "Mean R", help: "Average result per trade across ALL coins, in R (1R = the amount risked). Negative = the rules lose money in general. This is the key number.", group: "train" },
  { key: "train_pooled_pf", label: "Profit factor", help: "Gross profit ÷ gross loss across all coins. Above 1.0 is profitable.", group: "train" },
  { key: "avg_cost_in_r", label: "Cost / trade", help: "Average fees + slippage per trade, in R.", group: "train" },
  { key: "selected", label: "Coins picked", help: "Coins chosen on 2023–24 results to carry into 2025.", group: "val" },
  { key: "val_positions", label: "Trades", help: "Positions on the picked coins during 2025 (data the selection never saw).", group: "val" },
  { key: "val_pooled_mean_r", label: "Mean R", help: "Average result per trade on the picked coins in 2025.", group: "val" },
  { key: "val_pooled_pf", label: "Profit factor", help: "Profit factor on the picked coins in 2025.", group: "val" },
  { key: "pct_selected_profitable_2025", label: "Coins profitable", help: "Share of picked coins that were still net profitable in 2025.", group: "val" },
  { key: "verdict", label: "Verdict", help: "PASS needs positive mean R, profit factor above the threshold and most picked coins profitable in 2025.", group: "" },
];

export default function StrategyLabPage() {
  const [trialCountK, setTrialCountK] = useState<number | null>(null);
  const [strategies, setStrategies] = useState<Array<{ name: string }>>([]);
  const [jobs, setJobs] = useState<StrategyJob[]>([]);
  const [activeJob, setActiveJob] = useState<StrategyJob | null>(null);
  const [selectedStrategy, setSelectedStrategy] = useState<string>("");
  const [comparisonReport, setComparisonReport] = useState<ComparisonRow[]>([]);
  const [selectedTfs, setSelectedTfs] = useState<string[]>(["5m", "15m", "30m", "1h", "2h", "4h"]);
  const [workerStatus, setWorkerStatus] = useState<{ status: string; current_job_id: string | null; queued_count: number } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Upload flow: 1) choose + check the file, 2) pick timeframes, 3) start the backtest.
  const [pendingUpload, setPendingUpload] = useState<StrategyUpload | null>(null);
  const [pendingFileName, setPendingFileName] = useState<string>("");
  const [evalMode, setEvalMode] = useState<"standard" | "walk_forward">("standard");
  const [isUploading, setIsUploading] = useState(false);
  const [isStarting, setIsStarting] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [isClearing, setIsClearing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [logModalJobId, setLogModalJobId] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [isLoadingLog, setIsLoadingLog] = useState(false);
  const [nowTs, setNowTs] = useState<number>(() => Date.now() / 1000);

  const fileInputRef = useRef<HTMLInputElement>(null);


  const loadStatus = async () => {
    try {
      const data = await api.strategyLabStatus();
      setTrialCountK(data.trial_count_k);
      setStrategies(data.strategies || []);
      setJobs(data.jobs || []);
      if (data.worker) {
        setWorkerStatus(data.worker);
      }

      // Pick active job if none
      if (!activeJob && data.jobs && data.jobs.length > 0) {
        const primary = data.jobs[0];
        setActiveJob(primary);
        setSelectedStrategy(primary.strategy_name);
      } else if (activeJob) {
        const refreshed = data.jobs.find((j: StrategyJob) => j.job_id === activeJob.job_id);
        setActiveJob(refreshed ?? null); // gone after "Clear everything"
      }

      const compRes = await api.strategyLabComparisonReport(selectedStrategy || undefined);
      setComparisonReport(compRes.report || []);
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  };

  // Timers always call the latest loadStatus (it reads activeJob / selectedStrategy).
  const loadRef = useRef(loadStatus);
  useEffect(() => {
    loadRef.current = loadStatus;
  });

  // Load now when the strategy changes, then refresh every 2s while any job is queued or
  // running (15s otherwise).
  // Nothing is polled while the tab is in the background; it refreshes when shown again.
  const hasRunningOrQueued = jobs.some((j) => ACTIVE_STATUSES.includes(j.status));
  useEffect(() => {
    const load = () => {
      if (!document.hidden) void loadRef.current();
    };
    const first = setTimeout(load, 0);
    const timer = setInterval(load, hasRunningOrQueued ? 2000 : 15000);
    document.addEventListener("visibilitychange", load);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
      document.removeEventListener("visibilitychange", load);
    };
  }, [selectedStrategy, hasRunningOrQueued]);

  // Clock for "last update Xs ago": ticks every second only while a job is queued or running.
  useEffect(() => {
    if (!hasRunningOrQueued) return;
    const timer = setInterval(() => {
      if (!document.hidden) setNowTs(Date.now() / 1000);
    }, 1000);
    return () => clearInterval(timer);
  }, [hasRunningOrQueued]);

  // Built-in strategies (strategy_lab/strategies/*.py), listed by the backend.
  const library = useApi(api.strategyLibrary);
  const [libraryPick, setLibraryPick] = useState<string>("");
  const pickedInfo = library.data?.find((s) => s.file === libraryPick) ?? null;

  // Step 1 (alternative): load a built-in strategy instead of uploading a file.
  const handlePickLibrary = async () => {
    if (!libraryPick) return;
    setIsUploading(true);
    setUploadError(null);
    setNotice(null);
    try {
      const res = await api.registerLibraryStrategy(libraryPick);
      setPendingUpload(res);
      setPendingFileName(`library: ${libraryPick}.py`);
      await loadStatus();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Could not load the strategy");
    } finally {
      setIsUploading(false);
    }
  };

  // Step 1: upload and check the file. No backtest starts yet.
  const handleChooseFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setIsUploading(true);
    setUploadError(null);
    setNotice(null);
    setPendingUpload(null);
    try {
      const res = await api.checkStrategyFile(file);
      setPendingUpload(res);
      setPendingFileName(file.name);
      await loadStatus();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  // Step 3: queue one backtest job per selected timeframe.
  const handleRunBacktest = async () => {
    if (!pendingUpload || selectedTfs.length === 0) return;
    setIsStarting(true);
    setUploadError(null);
    try {
      const res = await api.startStrategyJobs(pendingUpload.strategy_id, selectedTfs, evalMode);
      const first = res.jobs[0]?.job_id;
      if (first) setActiveJob(await api.strategyLabJob(first));
      setSelectedStrategy(pendingUpload.name);
      setNotice(`Backtest started: ${pendingUpload.name} [${evalMode === "walk_forward" ? "Walk-Forward" : "Standard"}] on ${selectedTfs.length} timeframe${selectedTfs.length > 1 ? "s" : ""}. Progress is shown live below.`);
      setPendingUpload(null);
      setPendingFileName("");
      await loadStatus();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Could not start the backtest");
    } finally {
      setIsStarting(false);
    }
  };

  const handleClearAll = async () => {
    setIsClearing(true);
    try {
      const res = await api.clearStrategyLab();
      setActiveJob(null);
      setSelectedStrategy("");
      setComparisonReport([]);
      setPendingUpload(null);
      setPendingFileName("");
      setConfirmClear(false);
      setNotice(`Cleared ${res.removed_jobs} job${res.removed_jobs === 1 ? "" : "s"}. Trial count K stays at ${res.trial_count_k}.`);
      await loadStatus();
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Clear failed");
      setConfirmClear(false);
    } finally {
      setIsClearing(false);
    }
  };
  const handleRetry = async (jobId: string) => {
    try {
      await api.retryJob(jobId);
      await loadStatus();
    } catch (err) {
      alert(`Retry failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleCancel = async (jobId: string) => {
    try {
      await api.cancelJob(jobId);
      await loadStatus();
    } catch (err) {
      alert(`Cancel failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleViewLog = async (jobId: string) => {
    setLogModalJobId(jobId);
    setIsLoadingLog(true);
    try {
      const res = await api.strategyLabJobLog(jobId, 50);
      setLogLines(res.lines || []);
    } catch {
      setLogLines(["Failed to load log lines"]);
    } finally {
      setIsLoadingLog(false);
    }
  };

  const selectJob = (job: StrategyJob) => {
    setActiveJob(job);
    setSelectedStrategy(job.strategy_name);
  };

  // ── Derived Strategy Names ───────────────────────────────────────────────
  const allStrategyNames = Array.from(
    new Set([...strategies.map((s) => s.name), ...jobs.map((j) => j.strategy_name)]),
  ).filter(Boolean);

  const currentStrat = selectedStrategy || allStrategyNames[0] || "";
  const stratJobs = jobs.filter((j) => j.strategy_name === currentStrat);
  const currentVersion = stratJobs[0]?.strategy_version;

  // Latest job per timeframe for the current strategy
  const tfJobMap: Record<string, StrategyJob> = {};
  for (const tf of ALL_TIMEFRAMES) {
    const match = stratJobs.find((j) => (j.timeframe || "4h") === tf);
    if (match) tfJobMap[tf] = match;
  }

  // ── Verdict summary (plain English) ──────────────────────────────────────
  const completedTfs = ORDERED_TIMEFRAMES.filter((tf) => tfJobMap[tf]?.status === "COMPLETED");
  const queuedTfs = ORDERED_TIMEFRAMES.filter((tf) => tfJobMap[tf]?.status === "QUEUED");
  const runningTfs = ORDERED_TIMEFRAMES.filter((tf) => RUNNING_STATUSES.includes(tfJobMap[tf]?.status || ""));
  const passTfs = completedTfs.filter((tf) => tfJobMap[tf]?.verdict === "PASS");
  const testedTfs = ORDERED_TIMEFRAMES.filter((tf) => tfJobMap[tf]);

  let verdict: { tone: Tone; headline: string; detail: string };
  if (completedTfs.length === 0) {
    verdict = {
      tone: "muted",
      headline: "No result yet",
      detail: testedTfs.length ? "Runs are queued or in progress. The verdict appears when the first timeframe finishes." : "Upload a strategy to start.",
    };
  } else if (passTfs.length > 0) {
    verdict = {
      tone: "up",
      headline: `Possible edge on ${passTfs.join(", ")}`,
      detail: "Passed the 2025 check on the picked coins. Still unproven: confirm on more data before trusting it.",
    };
  } else {
    verdict = {
      tone: "down",
      headline: `No edge on the ${completedTfs.length} timeframe${completedTfs.length > 1 ? "s" : ""} finished so far`,
      detail: `Every finished run (${completedTfs.join(", ")}) failed the checks: the rules lose money or are not reliable across coins.`,
    };
  }
  const verdictStyles = {
    up: "border-up/40 bg-up/[0.07]",
    down: "border-down/40 bg-down/[0.07]",
    muted: "border-border bg-surface",
    accent: "border-accent/40 bg-accent/[0.07]",
    warn: "border-warn/40 bg-warn/[0.07]",
  }[verdict.tone];
  const verdictText = { up: "text-up", down: "text-down", muted: "text-foreground", accent: "text-accent", warn: "text-warn" }[verdict.tone];

  const activeStatus = activeJob ? jobStatus(activeJob, nowTs) : null;

  // Running first, then the queue in the order it will run.
  const liveJobs = jobs
    .filter((j) => ACTIVE_STATUSES.includes(j.status))
    .sort((a, b) => Number(a.status === "QUEUED") - Number(b.status === "QUEUED") || a.created_at.localeCompare(b.created_at));

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader
        title="Strategy Lab"
        subtitle="Upload a strategy file. It is checked, run on every liquid coin for 2023–24 (TRAIN), the best coins are picked, and only those are tested on 2025 data the selection never saw."
      >
        <button type="button" onClick={() => setConfirmClear(true)} disabled={jobs.length === 0 && allStrategyNames.length === 0} className="btn btn-danger">
          Clear everything
        </button>
      </PageHeader>

      {notice && (
        <div className="flex items-center justify-between gap-3 rounded-md border border-accent/40 bg-accent/10 px-3 py-2 text-xs text-accent">
          {notice}
          <button onClick={() => setNotice(null)} className="text-accent/80 hover:text-accent" aria-label="Dismiss">
            ✕
          </button>
        </div>
      )}

      {loadError && (
        <div role="alert" className="rounded-md border border-down/40 bg-down/10 p-3 text-xs text-down">
          Backend request failed: {loadError}. Nothing is shown in place of the missing data.
        </div>
      )}

      {/* ── Top row: upload + desk counters ───────────────────────────── */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_auto_auto]">
        <section className="rounded-lg border border-border bg-surface p-5">
          <h2 className="font-display text-[15px] font-semibold">Test a strategy</h2>
          <ol className="mt-4 space-y-4">
            {/* Step 1: file */}
            <li className="flex gap-3">
              <StepDot n={1} done={!!pendingUpload} />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">Choose a strategy</div>
                {pendingUpload ? (
                  <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
                    <Badge tone="up">File OK</Badge>
                    <span className="font-semibold">
                      {pendingUpload.name} <span className="font-mono text-muted">v{pendingUpload.version}</span>
                    </span>
                    <span className="font-mono text-muted">
                      {pendingFileName} · {pendingUpload.short_hash}
                    </span>
                    {!pendingUpload.is_new_upload && <span className="text-muted">(same file as an earlier upload)</span>}
                    <button type="button" className="btn btn-sm" onClick={() => setPendingUpload(null)}>
                      Change
                    </button>
                  </div>
                ) : (
                  <div className="mt-1.5 flex flex-wrap items-center gap-2">
                    <label className={`btn ${isUploading ? "pointer-events-none opacity-50" : ""}`}>
                      {isUploading ? "Checking file…" : "Choose .py file"}
                      <input ref={fileInputRef} type="file" accept=".py" onChange={handleChooseFile} disabled={isUploading} className="sr-only" />
                    </label>
                    <span className="text-xs text-muted">Written from the template. It is checked before anything runs.</span>
                  </div>
                )}
                {!pendingUpload && (
                  <div className="mt-3 border-t border-border pt-3">
                    <div className="label-caps mb-1.5">Or pick a built-in strategy</div>
                    {library.error ? (
                      <p className="text-xs text-down">Could not load the library: {library.error}</p>
                    ) : !library.data ? (
                      <p className="text-xs text-muted">Loading…</p>
                    ) : library.data.length === 0 ? (
                      <p className="text-xs text-muted">No built-in strategies.</p>
                    ) : (
                      <div className="flex flex-wrap items-center gap-2">
                        <select
                          value={libraryPick}
                          onChange={(e) => setLibraryPick(e.target.value)}
                          className="min-w-0 max-w-full px-2 py-1.5 text-sm"
                          aria-label="Built-in strategy"
                        >
                          <option value="">Select a strategy…</option>
                          {library.data.map((s) => (
                            <option key={s.file} value={s.file} disabled={!!s.error}>
                              {s.name ?? s.file}
                              {s.version ? ` v${s.version}` : ""}
                              {s.error ? " (broken)" : ""}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          onClick={() => void handlePickLibrary()}
                          disabled={!libraryPick || isUploading}
                          className="btn"
                        >
                          {isUploading ? "Loading…" : "Use this strategy"}
                        </button>
                      </div>
                    )}
                    {pickedInfo && (
                      <div className="mt-2 rounded-md border border-border bg-background/60 p-2.5 text-xs">
                        {pickedInfo.error ? (
                          <span className="text-down">{pickedInfo.error}</span>
                        ) : (
                          <>
                            <div className="text-foreground">{pickedInfo.title}</div>
                            {pickedInfo.description && <p className="mt-1 leading-relaxed text-muted">{pickedInfo.description}</p>}
                            {pickedInfo.params && (
                              <div className="mt-1.5 flex flex-wrap gap-1">
                                {Object.entries(pickedInfo.params).map(([k, v]) => (
                                  <span key={k} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-muted">
                                    {k}={String(v)}
                                  </span>
                                ))}
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </li>

            {/* Step 2: evaluation mode */}
            <li className={`flex gap-3 ${pendingUpload ? "" : "opacity-50"}`}>
              <StepDot n={2} done={!!pendingUpload} />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">Evaluation mode</div>
                <div className="mt-1.5 grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <button
                    type="button"
                    disabled={!pendingUpload}
                    onClick={() => setEvalMode("standard")}
                    className={`rounded-md border p-2.5 text-left transition-colors ${
                      evalMode === "standard"
                        ? "border-accent/70 bg-accent/10 text-foreground ring-1 ring-accent/50"
                        : "border-border bg-background/50 text-muted hover:border-border-strong hover:text-foreground"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-xs text-foreground">Standard (Phase 2)</span>
                      {evalMode === "standard" && <Badge tone="accent">Active</Badge>}
                    </div>
                    <div className="mt-1 text-[11px] leading-relaxed text-muted">
                      Train on 2023–2024 (24m), validate on 2025 (12m).
                    </div>
                  </button>
                  <button
                    type="button"
                    disabled={!pendingUpload}
                    onClick={() => setEvalMode("walk_forward")}
                    className={`rounded-md border p-2.5 text-left transition-colors ${
                      evalMode === "walk_forward"
                        ? "border-accent/70 bg-accent/10 text-foreground ring-1 ring-accent/50"
                        : "border-border bg-background/50 text-muted hover:border-border-strong hover:text-foreground"
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-xs text-foreground">Walk-Forward (8 Quarters)</span>
                      {evalMode === "walk_forward" && <Badge tone="accent">Active</Badge>}
                    </div>
                    <div className="mt-1 text-[11px] leading-relaxed text-muted">
                      Rolling 12m selection → 3m test (2024 Q1 → 2025 Q4).
                    </div>
                  </button>
                </div>
              </div>
            </li>

            {/* Step 3: timeframes */}
            <li className={`flex gap-3 ${pendingUpload ? "" : "opacity-50"}`}>
              <StepDot n={3} done={!!pendingUpload && selectedTfs.length > 0} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2 text-sm font-medium">
                  Pick the timeframes
                  <button
                    type="button"
                    disabled={!pendingUpload}
                    onClick={() => setSelectedTfs(selectedTfs.length === ALL_TIMEFRAMES.length ? [] : [...ALL_TIMEFRAMES])}
                    className="text-xs font-normal text-accent hover:underline disabled:no-underline"
                  >
                    {selectedTfs.length === ALL_TIMEFRAMES.length ? "Select none" : "Select all"}
                  </button>
                </div>
                <div className="mt-1.5 flex flex-wrap gap-1.5" role="group" aria-label="Timeframes to run">
                  {ORDERED_TIMEFRAMES.map((tf) => {
                    const on = selectedTfs.includes(tf);
                    return (
                      <button
                        key={tf}
                        type="button"
                        aria-pressed={on}
                        disabled={!pendingUpload}
                        onClick={() => setSelectedTfs(on ? selectedTfs.filter((t) => t !== tf) : [...selectedTfs, tf])}
                        className={`rounded-md border px-2.5 py-1 font-mono text-xs transition-colors ${
                          on ? "border-accent/60 bg-accent/15 text-accent" : "border-border text-muted hover:text-foreground"
                        }`}
                      >
                        {tf}
                      </button>
                    );
                  })}
                </div>
                <p className="mt-1 text-[11px] text-muted">Each timeframe is a separate run. Small timeframes (5m) take the longest.</p>
              </div>
            </li>

            {/* Step 4: run */}
            <li className={`flex gap-3 ${pendingUpload ? "" : "opacity-50"}`}>
              <StepDot n={4} done={false} />
              <div className="min-w-0 flex-1">
                <button
                  type="button"
                  onClick={() => void handleRunBacktest()}
                  disabled={!pendingUpload || selectedTfs.length === 0 || isStarting}
                  className="btn btn-primary"
                >
                  {isStarting
                    ? "Starting…"
                    : selectedTfs.length === 0
                      ? "Pick at least one timeframe"
                      : `Run ${evalMode === "walk_forward" ? "walk-forward" : "backtest"} on ${selectedTfs.length} timeframe${selectedTfs.length > 1 ? "s" : ""}`}
                </button>
              </div>
            </li>
          </ol>
          {uploadError && (
            <div role="alert" className="mt-4 rounded-md border border-down/40 bg-down/10 px-3 py-2 text-xs text-down">
              {uploadError}
            </div>
          )}
        </section>
        <Stat
          label="Trials so far (K)"
          value={trialCountK ?? <span className="font-sans text-xs font-normal text-muted">No data yet</span>}
          hint="+1 for each new strategy × timeframe run. More trials = more chance a pass is luck."
        />

        <div className="rounded-lg border border-border bg-surface px-4 py-3">
          <div className="label-caps">Worker</div>
          {workerStatus ? (
            <>
              <div className="mt-1.5 flex items-center gap-2">
                <span className={`h-2 w-2 rounded-full ${workerStatus.status === "running" ? "bg-up" : "bg-down"}`} />
                <span className="font-display text-lg font-semibold capitalize">{workerStatus.status}</span>
              </div>
              <div className="mt-0.5 text-[11px] text-muted">
                {workerStatus.queued_count > 0 ? `${workerStatus.queued_count} job${workerStatus.queued_count > 1 ? "s" : ""} waiting` : "Nothing waiting"}
              </div>
            </>
          ) : (
            <div className="mt-1.5 text-xs text-muted">No data yet</div>
          )}
        </div>
      </div>

      {/* ── Live progress of queued / running backtests ───────────────── */}
      {liveJobs.length > 0 && (
        <Card
          title={
            <span className="flex items-center gap-2">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
              </span>
              Backtests in progress
            </span>
          }
          right={<span className="text-xs text-muted">Updates every 2 s</span>}
        >
          <div className="space-y-3">
            {liveJobs.map((job) => (
              <LiveRun
                key={job.job_id}
                job={job}
                nowTs={nowTs}
                selected={activeJob?.job_id === job.job_id}
                onSelect={() => selectJob(job)}
                onCancel={() => void handleCancel(job.job_id)}
              />
            ))}
          </div>
        </Card>
      )}

      {/* ── Verdict for the selected strategy ─────────────────────────── */}
      {allStrategyNames.length > 0 && (
        <section className={`rounded-lg border p-5 ${verdictStyles}`}>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="min-w-0">
              <div className="label-caps">
                Verdict · {currentStrat}
                {currentVersion && <span className="ml-1 font-mono normal-case tracking-normal">v{currentVersion}</span>}
              </div>
              <div className={`mt-1 font-display text-2xl font-bold ${verdictText}`}>{verdict.headline}</div>
              <p className="mt-1 max-w-2xl text-sm text-muted">{verdict.detail}</p>
            </div>
            {allStrategyNames.length > 1 && (
              <label className="flex items-center gap-2 text-xs text-muted">
                Strategy
                <select value={currentStrat} onChange={(e) => setSelectedStrategy(e.target.value)} className="px-2.5 py-1.5 text-sm">
                  {allStrategyNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {/* Timeframe strip: one tile per timeframe for this strategy */}
          <div className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-6">
            {ORDERED_TIMEFRAMES.map((tf) => {
              const job = tfJobMap[tf];
              const st = job ? jobStatus(job, nowTs) : null;
              const selected = job && activeJob?.job_id === job.job_id;
              return (
                <button
                  key={tf}
                  type="button"
                  disabled={!job}
                  onClick={() => job && selectJob(job)}
                  className={`rounded-md border bg-background/70 px-3 py-2 text-left transition-colors disabled:cursor-default ${
                    selected ? "border-accent ring-1 ring-accent" : "border-border hover:border-border-strong"
                  }`}
                >
                  <div className="font-mono text-sm font-semibold">{tf}</div>
                  <div className="mt-1">{st ? <Badge tone={st.tone}>{st.label}</Badge> : <span className="text-[11px] text-muted">Not run</span>}</div>
                </button>
              );
            })}
          </div>
          {(runningTfs.length > 0 || queuedTfs.length > 0) && (
            <p className="mt-3 text-xs text-muted">
              {runningTfs.length > 0 && <>Running: {runningTfs.join(", ")}. </>}
              {queuedTfs.length > 0 && <>Waiting: {queuedTfs.join(", ")}.</>}
            </p>
          )}
        </section>
      )}

      {/* ── Selected run details ──────────────────────────────────────── */}
      {!activeJob || !activeStatus ? (
        <Empty>No run selected. Pick a timeframe tile above or a row in the job queue below to see its details.</Empty>
      ) : (
        <section className="space-y-4 rounded-lg border border-border bg-surface p-5">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="label-caps">Run details</div>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <span className="font-display text-xl font-bold">
                  {activeJob.strategy_name} · {activeJob.timeframe || "4h"}
                </span>
                <Badge tone={activeStatus.tone}>{activeStatus.label}</Badge>
              </div>
              <div className="mt-1 font-mono text-[11px] text-muted">
                v{activeJob.strategy_version} · trial #{activeJob.trial_k} · job {activeJob.job_id} · created {activeJob.created_at}
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <button onClick={() => handleViewLog(activeJob.job_id)} className="btn">
                View log
              </button>
              {activeJob.status === "COMPLETED" && (
                <>
                  <a href={`${API_URL}/api/strategy-lab/jobs/${activeJob.job_id}/download/csv`} download className="btn">
                    Download CSV
                  </a>
                  <a href={`${API_URL}/api/strategy-lab/jobs/${activeJob.job_id}/download/json`} download className="btn">
                    Download JSON
                  </a>
                </>
              )}
            </div>
          </div>

          {activeJob.reject_reason && (
            <div className="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn">
              <span className="font-semibold">Why it stopped: </span>
              {activeJob.reject_reason}
            </div>
          )}

          {ACTIVE_STATUSES.includes(activeJob.status) && (
            <div className="space-y-1.5">
              <div className="flex justify-between text-xs text-muted">
                <span>
                  Step: <span className="text-foreground">{activeJob.current_step}</span>
                  {activeJob.current_coin && (
                    <>
                      {" "}
                      · coin <span className="font-mono text-foreground">{activeJob.current_coin}</span>
                    </>
                  )}
                </span>
                <span className="font-mono">
                  {activeJob.coins_total ? `${activeJob.coins_done ?? 0}/${activeJob.coins_total} coins · ` : ""}
                  {activeJob.progress_pct}%
                </span>
              </div>
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
                <div className="h-full rounded-full bg-accent transition-all duration-300" style={{ width: `${activeJob.progress_pct}%` }} />
              </div>
            </div>
          )}

          {activeJob.status !== "COMPLETED" ? (
            <Empty>
              {ACTIVE_STATUSES.includes(activeJob.status)
                ? "The evaluation results and rule checks appear when this run finishes."
                : `This run stopped before finishing (${activeJob.status.toLowerCase()}), so there are no results. ${
                    activeJob.status === "FAILED" ? "Open the log to see the error, then retry from the job queue." : ""
                  }`}
            </Empty>
          ) : activeJob.walkforward_rows && activeJob.walkforward_rows.length > 0 ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <div className="label-caps">Walk-Forward Evaluation · 8 Rolling Quarters</div>
                  <p className="text-xs text-muted">
                    Selection window: 12 months (rules_v1 scaled) → Out-of-sample test: 3 months.
                  </p>
                </div>
                <Badge tone={activeJob.verdict === "PASS" ? "up" : "down"}>
                  Verdict: {activeJob.verdict}
                </Badge>
              </div>

              <div className="-mx-5 overflow-x-auto">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Quarter</th>
                      <th>Selection (12m)</th>
                      <th>Test (3m OOS)</th>
                      <th>Coins Selected</th>
                      <th>Positions</th>
                      <th>Mean R</th>
                      <th>Profit Factor</th>
                      <th>Net Profit</th>
                      <th>Quarter Result</th>
                    </tr>
                  </thead>
                  <tbody className="font-mono text-xs">
                    {activeJob.walkforward_rows.map((row) => (
                      <tr key={row.quarter_id}>
                        <td className="font-semibold text-foreground">{row.quarter}</td>
                        <td className="text-muted">{row.train_start} → {row.train_end}</td>
                        <td className="text-muted">{row.test_start} → {row.test_end}</td>
                        <td>
                          <span title={row.coins.join(", ")} className="cursor-help underline decoration-dotted">
                            {row.coins_selected} coins
                          </span>
                        </td>
                        <td>{val(row.positions)}</td>
                        <td className={`font-semibold ${toneOf(row.mean_r, 0)}`}>{signed(row.mean_r)}</td>
                        <td className={toneOf(row.profit_factor, 1)}>{val(row.profit_factor)}</td>
                        <td className={`font-semibold ${toneOf(row.profit, 0)}`}>
                          {row.profit >= 0 ? `+$${row.profit.toFixed(2)}` : `-$${Math.abs(row.profit).toFixed(2)}`}
                        </td>
                        <td>
                          <Badge tone={row.is_profitable ? "up" : "down"}>
                            {row.is_profitable ? "Profitable" : "Unprofitable"}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                    {activeJob.walkforward_combined && (
                      <tr className="border-t-2 border-border bg-surface-2/60 font-semibold">
                        <td className="text-foreground">{activeJob.walkforward_combined.quarter}</td>
                        <td colSpan={2} className="text-muted">8 Rolling Out-of-Sample Quarters</td>
                        <td>{activeJob.walkforward_combined.coins_selected} unique</td>
                        <td>{val(activeJob.walkforward_combined.positions)}</td>
                        <td className={toneOf(activeJob.walkforward_combined.mean_r, 0)}>
                          {signed(activeJob.walkforward_combined.mean_r)}
                        </td>
                        <td className={toneOf(activeJob.walkforward_combined.profit_factor, 1.2)}>
                          {val(activeJob.walkforward_combined.profit_factor)}
                        </td>
                        <td className={toneOf(activeJob.walkforward_combined.profit, 0)}>
                          {activeJob.walkforward_combined.profit >= 0
                            ? `+$${activeJob.walkforward_combined.profit.toFixed(2)}`
                            : `-$${Math.abs(activeJob.walkforward_combined.profit).toFixed(2)}`}
                        </td>
                        <td>
                          <Badge tone={activeJob.verdict === "PASS" ? "up" : "down"}>
                            {activeJob.verdict}
                          </Badge>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {activeJob.rules_verdict && Object.keys(activeJob.rules_verdict).length > 0 && (
                <div>
                  <div className="label-caps mb-2">Frozen Rules (rules_walkforward_v1.yaml)</div>
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                    {Object.entries(activeJob.rules_verdict).map(([k, rule]) => (
                      <div
                        key={k}
                        className={`rounded-md border px-3 py-2.5 ${
                          rule.passed ? "border-up/35 bg-up/[0.06]" : "border-down/35 bg-down/[0.06]"
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-semibold capitalize">{k.replace(/_/g, " ")}</span>
                          <Badge tone={rule.passed ? "up" : "down"}>{rule.passed ? "Pass" : "Fail"}</Badge>
                        </div>
                        <div className="mt-1 font-mono text-lg font-semibold">
                          {typeof rule.value === "number" ? rule.value : String(rule.value)}
                        </div>
                        <div className="text-[11px] text-muted">{rule.rule}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <>
              {/* Funnel: how many coins survive each stage */}
              <div>
                <div className="label-caps mb-2">Coin funnel</div>
                <ol className="grid grid-cols-2 gap-2 md:grid-cols-4">
                  {[
                    { label: "In universe", value: activeJob.funnel?.total_universe, note: "all coins considered" },
                    { label: "Eligible", value: activeJob.funnel?.eligible_coins, note: "enough liquidity + data" },
                    { label: "Picked on 2023–24", value: activeJob.funnel?.selected_coins, note: "best TRAIN results" },
                    { label: "Profitable in 2025", value: activeJob.funnel?.profitable_2025, note: "unseen data", tone: "text-up" },
                  ].map((s, i) => (
                    <li key={s.label} className="relative rounded-md border border-border bg-background/60 px-3 py-2.5">
                      <div className="label-caps">{s.label}</div>
                      <div className={`mt-0.5 font-mono text-2xl font-semibold ${s.tone ?? ""}`}>{val(s.value)}</div>
                      <div className="text-[11px] text-muted">{s.note}</div>
                      {i < 3 && <span aria-hidden className="absolute top-1/2 -right-2 z-10 hidden -translate-y-1/2 text-muted md:block">›</span>}
                    </li>
                  ))}
                </ol>
              </div>

              {activeJob.rules_verdict && Object.keys(activeJob.rules_verdict).length > 0 && (
                <div>
                  <div className="label-caps mb-2">Rule checks (all must pass)</div>
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-2 lg:grid-cols-4">
                    {Object.entries(activeJob.rules_verdict).map(([k, rule]) => (
                      <div key={k} className={`rounded-md border px-3 py-2.5 ${rule.passed ? "border-up/35 bg-up/[0.06]" : "border-down/35 bg-down/[0.06]"}`}>
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-semibold capitalize">{k.replace(/_/g, " ")}</span>
                          <Badge tone={rule.passed ? "up" : "down"}>{rule.passed ? "Pass" : "Fail"}</Badge>
                        </div>
                        <div className="mt-1 font-mono text-lg font-semibold">{typeof rule.value === "number" ? rule.value : String(rule.value)}</div>
                        <div className="text-[11px] text-muted">{rule.rule}</div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </section>
      )}

      {/* ── Comparison across timeframes ──────────────────────────────── */}
      <Card
        title={`Compare timeframes${currentStrat ? ` · ${currentStrat}` : ""}`}
        right={
          currentStrat ? (
            <a href={`${API_URL}/api/strategy-lab/comparison-report.csv?strategy_id=${currentStrat}`} download="comparison_report.csv" className="btn">
              Download CSV
            </a>
          ) : undefined
        }
      >
        <p className="-mt-1 text-xs text-muted">
          Read the <span className="text-foreground">TRAIN mean R</span> first: it is the average result per trade over all coins, so a negative value means the rules
          lose money in general, whatever a few lucky coins did. Hover a column name for its definition.
        </p>
        <div className="-mx-5 overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th colSpan={1} className="!bg-surface" />
                <th colSpan={5} className="border-l border-border !bg-surface text-center !text-foreground">
                  TRAIN · 2023–24 · all coins
                </th>
                <th colSpan={5} className="border-l border-border !bg-surface text-center !text-foreground">
                  VALIDATION · 2025 · picked coins only
                </th>
                <th className="border-l border-border !bg-surface" />
              </tr>
              <tr>
                {COMPARISON_COLUMNS.map((c, i) => {
                  const startsGroup = i > 0 && COMPARISON_COLUMNS[i - 1].group !== c.group;
                  return (
                    <th
                      key={c.key}
                      title={c.help}
                      className={`cursor-help ${startsGroup ? "border-l border-border" : ""} ${c.key === "train_pooled_mean_r" ? "!text-accent" : ""}`}
                    >
                      <span className="underline decoration-dotted underline-offset-4">{c.label}</span>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody className="font-mono">
              {comparisonReport.length === 0 ? (
                <tr>
                  <td colSpan={COMPARISON_COLUMNS.length} className="py-8 text-center font-sans text-muted">
                    No finished runs to compare yet{currentStrat ? ` for ${currentStrat}` : ""}.
                  </td>
                </tr>
              ) : (
                comparisonReport.map((r, i) => (
                  <tr key={r.tf || i}>
                    <td className="font-semibold text-foreground">{r.tf}</td>
                    <td className="border-l border-border">{val(r.eligible_coins)}</td>
                    <td>{val(r.train_positions)}</td>
                    <td className={`bg-accent/[0.06] font-semibold ${toneOf(r.train_pooled_mean_r, 0)}`}>{signed(r.train_pooled_mean_r)}</td>
                    <td className={toneOf(r.train_pooled_pf, 1)}>{val(r.train_pooled_pf)}</td>
                    <td>{r.avg_cost_in_r != null ? `${r.avg_cost_in_r} R` : val(null)}</td>
                    <td className="border-l border-border">{val(r.selected)}</td>
                    <td>{val(r.val_positions)}</td>
                    <td className={`font-semibold ${toneOf(r.val_pooled_mean_r, 0)}`}>{signed(r.val_pooled_mean_r)}</td>
                    <td className={toneOf(r.val_pooled_pf, 1)}>{val(r.val_pooled_pf)}</td>
                    <td>{r.pct_selected_profitable_2025 != null ? `${r.pct_selected_profitable_2025}%` : val(null)}</td>
                    <td className="border-l border-border font-sans">
                      {r.verdict ? <Badge tone={r.verdict === "PASS" ? "up" : "down"}>{r.verdict === "PASS" ? "Pass" : "Fail"}</Badge> : val(null)}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* ── All strategies × timeframes ───────────────────────────────── */}
      {allStrategyNames.length > 0 && (
        <Card title="All strategies" right={<span className="font-mono text-xs text-muted">{jobs.length} jobs</span>}>
          <div className="-mx-5 overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Strategy</th>
                  {ORDERED_TIMEFRAMES.map((tf) => (
                    <th key={tf} className="text-center font-mono normal-case">
                      {tf}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {allStrategyNames.map((stratName) => {
                  const sJobs = jobs.filter((j) => j.strategy_name === stratName);
                  return (
                    <tr key={stratName}>
                      <td>
                        <button
                          onClick={() => setSelectedStrategy(stratName)}
                          className={`font-semibold hover:underline ${currentStrat === stratName ? "text-accent" : "text-foreground"}`}
                        >
                          {stratName}
                        </button>
                      </td>
                      {ORDERED_TIMEFRAMES.map((tf) => {
                        const job = sJobs.find((j) => (j.timeframe || "4h") === tf);
                        if (!job) {
                          return (
                            <td key={tf} className="text-center text-muted/50">
                              —
                            </td>
                          );
                        }
                        const st = jobStatus(job, nowTs);
                        return (
                          <td key={tf} className="text-center">
                            <button
                              onClick={() => selectJob(job)}
                              className={`rounded px-1 py-0.5 ${activeJob?.job_id === job.job_id ? "ring-1 ring-accent" : ""}`}
                            >
                              <Badge tone={st.tone}>{st.label}</Badge>
                            </button>
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] leading-relaxed text-muted">
            <span className="text-foreground">Fail · no edge</span> = the run finished and the strategy did not pass.{" "}
            <span className="text-foreground">Run error</span> = the run itself crashed (see its log).{" "}
            <span className="text-foreground">Rejected</span> = the file failed the safety checks before running.{" "}
            <span className="text-foreground">Stalled</span> = no progress for 2 minutes.
          </p>
        </Card>
      )}

      {/* ── Job queue ─────────────────────────────────────────────────── */}
      <Card title="Job queue" right={<button onClick={() => loadStatus()} className="btn">Refresh</button>}>
        <div className="-mx-5 overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Status</th>
                <th>Progress</th>
                <th>Coin</th>
                <th>Created</th>
                <th>Elapsed</th>
                <th>Time left</th>
                <th>Last update</th>
                <th className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.length === 0 ? (
                <tr>
                  <td colSpan={9} className="py-8 text-center text-muted">
                    No jobs yet. Upload a strategy above to start one.
                  </td>
                </tr>
              ) : (
                jobs.slice(0, 20).map((job) => {
                  const st = jobStatus(job, nowTs);
                  const isSelected = activeJob?.job_id === job.job_id;
                  return (
                    <tr key={job.job_id} className={`cursor-pointer ${isSelected ? "[&>td]:bg-surface-2" : ""}`} onClick={() => selectJob(job)}>
                      <td>
                        <div className="flex items-center gap-1.5 font-semibold">
                          {job.strategy_name}
                          <span className="rounded bg-surface-2 px-1.5 font-mono text-[10px] text-muted">{job.timeframe || "4h"}</span>
                        </div>
                        <div className="font-mono text-[10px] text-muted">{job.job_id}</div>
                      </td>
                      <td>
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </td>
                      <td>
                        <div className="flex w-28 items-center gap-2">
                          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2">
                            <div className="h-full rounded-full bg-accent transition-all duration-300" style={{ width: `${job.progress_pct}%` }} />
                          </div>
                          <span className="w-8 text-right font-mono text-[10px] text-muted">{job.progress_pct}%</span>
                        </div>
                      </td>
                      <td className="font-mono text-muted">{job.current_coin || "—"}</td>
                      <td className="whitespace-nowrap font-mono text-muted">{job.created_at.replace(" UTC", "")}</td>
                      <td className="whitespace-nowrap font-mono">{fmtDuration(job.elapsed_seconds)}</td>
                      <td className="whitespace-nowrap font-mono text-muted">{job.estimated_seconds && ACTIVE_STATUSES.includes(job.status) ? `~${fmtDuration(job.estimated_seconds)}` : "—"}</td>
                      <td className={`whitespace-nowrap font-mono ${st.stalled ? "font-semibold text-down" : "text-muted"}`}>
                        {fmtDuration(Math.max(1, st.ageS))} ago
                      </td>
                      <td className="text-right" onClick={(e) => e.stopPropagation()}>
                        <div className="flex items-center justify-end gap-1.5">
                          <button onClick={() => handleViewLog(job.job_id)} className="btn btn-sm">
                            Log
                          </button>
                          {(st.stalled || job.status === "FAILED" || job.status === "CANCELLED") && (
                            <button onClick={() => handleRetry(job.job_id)} className="btn btn-sm">
                              Retry
                            </button>
                          )}
                          {ACTIVE_STATUSES.includes(job.status) && (
                            <button onClick={() => handleCancel(job.job_id)} className="btn btn-sm btn-danger">
                              Cancel
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* ── Clear confirmation ─────────────────────────────────────────── */}
      {confirmClear && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" role="dialog" aria-modal="true">
          <div className="w-full max-w-md rounded-lg border border-border bg-surface p-6 shadow-2xl">
            <h2 className="font-display text-lg font-bold">Clear everything?</h2>
            <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-muted">
              <li>Stops running backtests and empties the queue.</li>
              <li>Deletes all {jobs.length} job{jobs.length === 1 ? "" : "s"} with their results and logs.</li>
              <li>Empties the strategy list. Your .py files stay on disk.</li>
              <li>
                <span className="text-foreground">Trial count K is kept ({trialCountK ?? "?"})</span>: it counts every test ever run, so it never goes back to 0.
              </li>
            </ul>
            <p className="mt-3 text-sm text-down">This cannot be undone.</p>
            <div className="mt-5 flex justify-end gap-2">
              <button onClick={() => setConfirmClear(false)} className="btn" disabled={isClearing}>
                Keep everything
              </button>
              <button onClick={() => void handleClearAll()} className="btn btn-danger" disabled={isClearing}>
                {isClearing ? "Clearing…" : "Clear everything"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Log Modal ───────────────────────────────────────────────────── */}
      {logModalJobId && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" role="dialog" aria-modal="true">
          <div className="flex max-h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-border bg-surface shadow-2xl">
            <div className="flex items-center justify-between border-b border-border px-5 py-3">
              <div className="font-display text-sm font-semibold">
                Log · job <span className="font-mono">{logModalJobId}</span>
              </div>
              <button onClick={() => setLogModalJobId(null)} className="btn btn-sm" aria-label="Close log">
                Close
              </button>
            </div>
            <div className="flex-1 space-y-0.5 overflow-y-auto bg-background p-4 font-mono text-xs text-foreground/90">
              {isLoadingLog ? (
                <div className="text-muted">Loading log…</div>
              ) : logLines.length === 0 ? (
                <div className="text-muted">No log lines yet for this job.</div>
              ) : (
                logLines.map((line, idx) => (
                  <div key={idx} className="whitespace-pre-wrap">
                    {line}
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const PHASE_LABELS: Record<string, string> = {
  QUEUED: "Waiting in queue",
  RUNNING_CHECKS: "Safety checks (look-ahead, accounting)",
  ESTIMATING: "Timing one coin to estimate duration",
  TRAIN_SCAN: "Testing 2023–24 on all coins",
  FREEZING: "Picking coins from 2023–24 results",
  VALIDATION_SCAN: "Testing 2025 on the picked coins",
};

function StepDot({ n, done }: { n: number; done: boolean }) {
  return (
    <span
      aria-hidden
      className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border font-mono text-[11px] ${
        done ? "border-up bg-up/15 text-up" : "border-border-strong text-muted"
      }`}
    >
      {done ? "✓" : n}
    </span>
  );
}

/** One queued or running backtest: overall %, coins tested so far, current coin, time. */
function LiveRun({
  job,
  nowTs,
  selected,
  onSelect,
  onCancel,
}: {
  job: StrategyJob;
  nowTs: number;
  selected: boolean;
  onSelect: () => void;
  onCancel: () => void;
}) {
  const st = jobStatus(job, nowTs);
  const queued = job.status === "QUEUED";
  const coinsTotal = job.coins_total ?? 0;
  const coinsDone = job.coins_done ?? 0;
  const coinName = job.current_coin?.split(" ").slice(1).join(" ");
  // Time left in the current coin scan from the real rate so far (coins done / time elapsed);
  // before the first coin, fall back to the backend's one-coin estimate.
  const remaining =
    coinsDone > 0 && coinsTotal > 0 && job.elapsed_seconds
      ? (job.elapsed_seconds * (coinsTotal - coinsDone)) / coinsDone
      : job.estimated_seconds && job.elapsed_seconds
        ? Math.max(0, job.estimated_seconds - job.elapsed_seconds)
        : null;
  return (
    <div className={`rounded-md border bg-background/60 p-3 ${selected ? "border-accent" : "border-border"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button onClick={onSelect} className="flex items-center gap-2 text-left">
          <span className="font-semibold">{job.strategy_name}</span>
          <span className="rounded bg-surface-2 px-1.5 font-mono text-xs">{job.timeframe || "4h"}</span>
          <Badge tone={st.tone}>{st.label}</Badge>
        </button>
        <div className="flex items-center gap-3">
          {!queued && (
            <span className="font-mono text-xs text-muted">
              {fmtDuration(job.elapsed_seconds)} elapsed{remaining ? ` · ~${fmtDuration(remaining)} left${job.scan_phase === "TRAIN" ? " in 2023–24 scan" : ""}` : ""}
            </span>
          )}
          <button onClick={onCancel} className="btn btn-sm btn-danger">
            Cancel
          </button>
        </div>
      </div>

      <div className="mt-2 text-xs text-muted">{PHASE_LABELS[job.status] ?? job.current_step}</div>

      {!queued && (
        <div className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
          <div>
            <div className="flex justify-between text-[11px] text-muted">
              <span>Overall</span>
              <span className="font-mono text-foreground">{job.progress_pct}%</span>
            </div>
            <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-surface-2">
              <div className="h-full rounded-full bg-accent transition-all duration-500" style={{ width: `${job.progress_pct}%` }} />
            </div>
          </div>
          <div className="text-right">
            <div className="label-caps">
              Coins tested{job.scan_phase ? ` · ${job.scan_phase === "TRAIN" ? "2023–24" : "2025"}` : ""}
            </div>
            <div className="font-mono text-lg font-semibold tabular-nums">
              {coinsTotal ? (
                <>
                  {coinsDone}
                  <span className="text-muted"> / {coinsTotal}</span>
                </>
              ) : (
                <span className="text-sm text-muted">not started</span>
              )}
            </div>
          </div>
        </div>
      )}
      {!queued && coinName && <div className="mt-1 font-mono text-[11px] text-muted">Last finished: {coinName}</div>}
    </div>
  );
}
