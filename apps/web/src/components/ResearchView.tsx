"use client";

import React, { useEffect, useState } from "react";

interface GateChecklist {
  min_trades_100: boolean;
  gross_expectancy_0_15R: boolean;
  net_expectancy_positive: boolean;
  beats_random_95th_pct: boolean;
  positive_symbols_60pct: boolean;
  both_halves_positive: boolean;
}

interface Hypothesis {
  id: string;
  name: string;
  status: "pre-registered" | "event-study" | "train" | "validation" | "rejected";
  trial_count: number;
  gate_checklist: GateChecklist;
  gate_passed: boolean;
  deflated_sharpe: number;
  event_study_summary: {
    total_signals: number;
    win_rate_pct: number;
    mfe_12_r: number;
    mae_12_r: number;
  };
  backtest_runs: string[];
}

interface ResearchData {
  hypotheses: Hypothesis[];
  validation_split_locked: boolean;
  train_period: string;
  validation_period: string;
}

export function ResearchView() {
  const [data, setData] = useState<ResearchData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/dashboard/research")
      .then((res) => res.json())
      .then((d) => {
        setData(d);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, []);

  if (loading) {
    return (
      <div className="p-8 text-gray-400 animate-pulse">Loading Research Framework...</div>
    );
  }

  if (error || !data) {
    return (
      <div className="p-8 text-red-400">Error loading research data: {error}</div>
    );
  }

  return (
    <div className="p-6 space-y-6 bg-slate-950 text-slate-100 min-h-screen font-sans">
      {/* Header Banner */}
      <div className="flex justify-between items-center bg-slate-900/80 p-6 rounded-xl border border-slate-800 shadow-xl backdrop-blur-md">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-white flex items-center gap-3">
            <span>🔬 Research & Hypothesis Framework</span>
          </h1>
          <p className="text-sm text-slate-400 mt-1">
            Pre-registered frozen parameter hypotheses, event studies, and encoded gate evaluation.
          </p>
        </div>
        <div className="flex items-center gap-4">
          <div className="text-right">
            <div className="text-xs text-slate-400">TRAIN PERIOD</div>
            <div className="text-sm font-mono text-cyan-400">{data.train_period}</div>
          </div>
          <div className="h-8 w-px bg-slate-800" />
          <div className="text-right">
            <div className="text-xs text-slate-400">VALIDATION SPLIT</div>
            <div className="text-sm font-mono text-emerald-400 flex items-center gap-1.5">
              <span>🔒 LOCKED</span>
              <span className="text-xs text-slate-400">({data.validation_period})</span>
            </div>
          </div>
        </div>
      </div>

      {/* Hypotheses Grid */}
      <div className="grid grid-cols-1 gap-6">
        {data.hypotheses.map((h) => (
          <div
            key={h.id}
            className="bg-slate-900/50 rounded-xl border border-slate-800/80 p-6 hover:border-slate-700 transition-all shadow-lg space-y-4"
          >
            {/* Top Bar */}
            <div className="flex justify-between items-start">
              <div>
                <div className="flex items-center gap-3">
                  <span className="px-2.5 py-1 text-xs font-mono font-semibold rounded bg-cyan-950 text-cyan-400 border border-cyan-800/50">
                    {h.id}
                  </span>
                  <h3 className="text-lg font-semibold text-white">{h.name}</h3>
                </div>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-slate-400 font-mono">
                  Trials: <strong className="text-white">{h.trial_count}</strong>
                </span>
                <span
                  className={`px-3 py-1 rounded-full text-xs font-semibold uppercase tracking-wider ${
                    h.status === "pre-registered"
                      ? "bg-amber-950/80 text-amber-300 border border-amber-800/60"
                      : h.status === "validation"
                      ? "bg-emerald-950/80 text-emerald-300 border border-emerald-800/60"
                      : "bg-slate-800 text-slate-300"
                  }`}
                >
                  {h.status}
                </span>
              </div>
            </div>

            {/* Research Gate Checklist */}
            <div className="bg-slate-950/60 p-4 rounded-lg border border-slate-800/60 space-y-2">
              <div className="text-xs font-semibold uppercase text-slate-400 tracking-wider flex justify-between">
                <span>Validation Gate Criteria (All Required)</span>
                <span>Deflated Sharpe: <strong className="text-cyan-400">{h.deflated_sharpe}</strong></span>
              </div>
              <div className="grid grid-cols-2 md:grid-cols-3 gap-3 pt-2 text-xs font-mono">
                <GateBadge label="≥ 100 Trades" passed={h.gate_checklist.min_trades_100} />
                <GateBadge label="Gross Exp ≥ 0.15R" passed={h.gate_checklist.gross_expectancy_0_15R} />
                <GateBadge label="Net Exp > 0" passed={h.gate_checklist.net_expectancy_positive} />
                <GateBadge label="≥ 95th % vs Random" passed={h.gate_checklist.beats_random_95th_pct} />
                <GateBadge label="≥ 60% Pos Symbols" passed={h.gate_checklist.positive_symbols_60pct} />
                <GateBadge label="Both Halves Pos" passed={h.gate_checklist.both_halves_positive} />
              </div>
            </div>

            {/* Summary Metrics */}
            <div className="grid grid-cols-4 gap-4 text-center bg-slate-950/30 p-3 rounded-lg border border-slate-800/30 text-xs">
              <div>
                <div className="text-slate-400">Total Signals</div>
                <div className="text-sm font-semibold font-mono text-white mt-0.5">
                  {h.event_study_summary.total_signals}
                </div>
              </div>
              <div>
                <div className="text-slate-400">Win Rate</div>
                <div className="text-sm font-semibold font-mono text-white mt-0.5">
                  {h.event_study_summary.win_rate_pct}%
                </div>
              </div>
              <div>
                <div className="text-slate-400">12-Bar MFE</div>
                <div className="text-sm font-semibold font-mono text-emerald-400 mt-0.5">
                  {h.event_study_summary.mfe_12_r}R
                </div>
              </div>
              <div>
                <div className="text-slate-400">12-Bar MAE</div>
                <div className="text-sm font-semibold font-mono text-rose-400 mt-0.5">
                  {h.event_study_summary.mae_12_r}R
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function GateBadge({ label, passed }: { label: string; passed: boolean }) {
  return (
    <div
      className={`flex items-center gap-2 p-2 rounded border ${
        passed
          ? "bg-emerald-950/40 border-emerald-800/50 text-emerald-300"
          : "bg-slate-900 border-slate-800 text-slate-500"
      }`}
    >
      <span>{passed ? "✓" : "✗"}</span>
      <span>{label}</span>
    </div>
  );
}
