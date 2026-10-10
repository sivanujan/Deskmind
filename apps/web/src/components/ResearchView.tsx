"use client";

import { API_URL } from "@/lib/api";
import { Badge, Card, ErrorBox, Loading, PageHeader, useApi } from "@/components/ui";

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

async function loadResearch(): Promise<ResearchData> {
  const res = await fetch(`${API_URL}/api/dashboard/research`, { credentials: "include" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

const GATES: Array<[keyof GateChecklist, string]> = [
  ["min_trades_100", "At least 100 trades"],
  ["gross_expectancy_0_15R", "Gross expectancy ≥ 0.15 R"],
  ["net_expectancy_positive", "Net expectancy > 0 (after costs)"],
  ["beats_random_95th_pct", "Beats 95% of random entries"],
  ["positive_symbols_60pct", "Positive on ≥ 60% of coins"],
  ["both_halves_positive", "Positive in both halves of the period"],
];

export function ResearchView() {
  const { data, error, loading } = useApi(loadResearch);

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader
        title="Hypotheses"
        subtitle="Trade ideas written down before testing, with their parameters frozen. Each one must clear every gate below before it can move to validation."
      >
        {data && (
          <div className="flex gap-6 text-right">
            <div>
              <div className="label-caps">Train period</div>
              <div className="font-mono text-sm">{data.train_period}</div>
            </div>
            <div>
              <div className="label-caps">Validation</div>
              <div className="font-mono text-sm">
                {data.validation_split_locked ? "Locked · " : ""}
                {data.validation_period}
              </div>
            </div>
          </div>
        )}
      </PageHeader>
      <ErrorBox error={error} />
      <Loading loading={loading} />

      {data?.hypotheses.map((h) => (
        <Card
          key={h.id}
          title={
            <span className="flex items-center gap-2">
              <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs text-muted">{h.id}</span>
              {h.name}
            </span>
          }
          right={
            <div className="flex items-center gap-3">
              <span className="text-xs text-muted">
                Trials <span className="font-mono text-foreground">{h.trial_count}</span>
              </span>
              <Badge tone={h.status === "validation" ? "up" : h.status === "rejected" ? "down" : h.status === "pre-registered" ? "warn" : "muted"}>
                {h.status}
              </Badge>
            </div>
          }
        >
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div>
              <div className="mb-2 flex justify-between">
                <span className="label-caps">Gates (all required)</span>
                <span className="text-xs text-muted">
                  Deflated Sharpe <span className="font-mono text-foreground">{h.deflated_sharpe}</span>
                </span>
              </div>
              <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                {GATES.map(([key, label]) => {
                  const passed = h.gate_checklist[key];
                  return (
                    <li
                      key={key}
                      className={`flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-xs ${
                        passed ? "border-up/35 bg-up/[0.06] text-foreground" : "border-border text-muted"
                      }`}
                    >
                      <span className={`font-mono ${passed ? "text-up" : "text-down"}`}>{passed ? "✓" : "✗"}</span>
                      {label}
                    </li>
                  );
                })}
              </ul>
            </div>
            <div>
              <div className="label-caps mb-2">Event study</div>
              <dl className="grid grid-cols-2 gap-2">
                {[
                  { k: "Signals", v: h.event_study_summary.total_signals, cls: "" },
                  { k: "Win rate", v: `${h.event_study_summary.win_rate_pct}%`, cls: "" },
                  { k: "Best move, 12 bars", v: `${h.event_study_summary.mfe_12_r} R`, cls: "text-up" },
                  { k: "Worst move, 12 bars", v: `${h.event_study_summary.mae_12_r} R`, cls: "text-down" },
                ].map((m) => (
                  <div key={m.k} className="rounded-md border border-border bg-background/60 px-2.5 py-1.5">
                    <dt className="text-[11px] text-muted">{m.k}</dt>
                    <dd className={`font-mono text-sm font-semibold ${m.cls}`}>{m.v}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}
