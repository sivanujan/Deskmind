"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, type BacktestJob, type BacktestRun } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, fmtTime, num, useApi, val } from "@/components/ui";

// Splits, strategies and defaults come from GET /api/dashboard/backtest-meta (config.BACKTEST_SPLITS,
// STRATEGY_REGISTRY). Runs come from reports/backtests/registry.json. Metric keys are the engine's own.

const METRIC_ROWS: Array<[string, string, number]> = [
  ["total_trades", "Trades", 0],
  ["win_rate_pct", "Win rate %", 1],
  ["expectancy_r", "Expectancy (R)", 3],
  ["profit_factor", "Profit factor", 2],
  ["net_return_usd", "Net return (USD)", 2],
  ["net_return_pct", "Net return %", 2],
  ["max_drawdown_pct", "Max drawdown %", 2],
  ["sharpe_ratio", "Sharpe", 2],
  ["sortino_ratio", "Sortino", 2],
  ["total_fees_paid", "Fees paid (USD)", 2],
  ["total_funding_paid", "Funding paid (USD)", 2],
  ["total_slippage_paid", "Slippage (USD)", 2],
  ["total_costs_usd", "Total costs (USD)", 2],
  ["skipped_signals_count", "Skipped signals", 0],
];

function Metrics({ m }: { m: Record<string, unknown> }) {
  return (
    <>
      {METRIC_ROWS.map(([key, label, d]) => (
        <KV key={key} k={label} v={key in m ? num(m[key], d) : val(null)} />
      ))}
    </>
  );
}

export default function BacktestPage() {
  const meta = useApi(api.backtestMeta);
  const runs = useApi(api.backtestRuns);
  const [strategy, setStrategy] = useState("");
  const [split, setSplit] = useState<"train" | "validation">("train");
  const [symbols, setSymbols] = useState<string[] | null>(null);
  const [warmup, setWarmup] = useState("");
  const [cash, setCash] = useState("");
  const [job, setJob] = useState<BacktestJob | null>(null);
  const [selected, setSelected] = useState<BacktestRun | null>(null);
  const [compare, setCompare] = useState<{ a: string; b: string }>({ a: "", b: "" });
  const [compared, setCompared] = useState<{ run_a: BacktestRun; run_b: BacktestRun } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [submitErr, setSubmitErr] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const m = meta.data;
  const strat = strategy || m?.strategies[0] || "";
  const syms = symbols ?? m?.defaults.symbols ?? [];

  useEffect(() => () => wsRef.current?.close(), []);

  const submit = async () => {
    setSubmitErr(null);
    try {
      const r = await api.submitBacktest({
        strategy: strat,
        split,
        symbols: syms,
        warmup_days: warmup ? Number(warmup) : undefined,
        initial_cash: cash || undefined,
      });
      const j = await api.backtestJobStatus(r.job_id);
      setJob(j);
      wsRef.current?.close();
      const ws = new WebSocket(api.backtestSocketUrl(r.job_id));
      wsRef.current = ws;
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data) as Partial<BacktestJob>;
        setJob((prev) => (prev ? { ...prev, ...msg } : prev));
        if (msg.status === "completed" || msg.status === "failed" || msg.status === "cancelled") {
          void runs.reload();
          if (msg.run_id) void api.backtestRunDetail(msg.run_id).then(setSelected);
        }
      };
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setSubmitErr(
        /authentication required|401/i.test(msg)
          ? "Log in first (Control Login, top right): starting a backtest is a control action."
          : `Could not start the backtest: ${msg}`,
      );
    }
  };

  const doCompare = async () => {
    setErr(null);
    try {
      setCompared(await api.compareRuns(compare.a, compare.b));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const runList = runs.data ? [...runs.data].reverse() : [];

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader title="Backtest" subtitle="Runs the real event-driven engine (same as `deskmind backtest run`). The test split is CLI-only." />
      <ErrorBox error={meta.error || runs.error || err} />
      <Loading loading={meta.loading} />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card title="New run">
          {!m && <Empty>No metadata yet.</Empty>}
          {m && (
            <div className="space-y-3 text-xs">
              <label className="block">
                <span className="text-muted">Strategy</span>
                <select value={strat} onChange={(e) => setStrategy(e.target.value)} className="mt-1 w-full rounded border border-border bg-background px-2 py-1">
                  {m.strategies.map((s) => (
                    <option key={s}>{s}</option>
                  ))}
                </select>
              </label>
              <div>
                <span className="text-muted">Split</span>
                <div className="mt-1 flex gap-2">
                  {Object.entries(m.splits).map(([name, sp]) => (
                    <button
                      key={name}
                      disabled={!sp.ui_allowed}
                      onClick={() => setSplit(name as "train" | "validation")}
                      title={sp.ui_allowed ? "" : "CLI only (locked holdout)"}
                      className={`flex-1 rounded border px-2 py-1 disabled:opacity-40 ${split === name ? "border-accent bg-accent/15 text-accent" : "border-border"}`}
                    >
                      {name}
                      <span className="block text-[10px] text-muted">
                        {sp.start} → {sp.end}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <span className="text-muted">Symbols (DATA_SYMBOLS)</span>
                <div className="mt-1 flex flex-wrap gap-2">
                  {m.defaults.symbols.map((s) => (
                    <label key={s} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={syms.includes(s)}
                        onChange={(e) => setSymbols(e.target.checked ? [...syms, s] : syms.filter((x) => x !== s))}
                      />
                      {s}
                    </label>
                  ))}
                </div>
              </div>
              <label className="block">
                <span className="text-muted">Warm-up days</span>
                <input
                  value={warmup}
                  onChange={(e) => setWarmup(e.target.value)}
                  placeholder={String(m.defaults.warmup_days)}
                  className="mt-1 w-full rounded border border-border bg-background px-2 py-1 font-mono"
                />
              </label>
              <label className="block">
                <span className="text-muted">Initial cash (USDT)</span>
                <input
                  value={cash}
                  onChange={(e) => setCash(e.target.value)}
                  placeholder={m.defaults.initial_cash}
                  className="mt-1 w-full rounded border border-border bg-background px-2 py-1 font-mono"
                />
              </label>
              <button
                disabled={!strat || syms.length === 0 || job?.status === "running" || job?.status === "queued"}
                onClick={() => void submit()}
                className="w-full rounded border border-accent/40 bg-accent/20 py-2 font-semibold text-accent disabled:opacity-40"
              >
                Run backtest
              </button>
              {submitErr && <div className="rounded border border-down/40 bg-down/10 p-2 text-down">{submitErr}</div>}
              {job && (
                <div className="rounded bg-surface-2 p-2">
                  <KV k="Job" v={job.job_id.slice(0, 8)} />
                  <KV k="Status" v={job.status} />
                  <KV k="Progress" v={job.status === "running" ? "running (engine reports no partial progress)" : `${job.progress_pct}%`} />
                  {job.error && <KV k="Error" v={<span className="text-down">{job.error}</span>} />}
                  {job.status === "running" && (
                    <button onClick={() => void api.cancelBacktest(job.job_id)} className="mt-1 text-down underline">
                      Cancel
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </Card>

        <Card title="Runs (registry.json)" className="lg:col-span-2">
          {runs.data && runs.data.length === 0 && <Empty>No backtest runs yet.</Empty>}
          <div className="max-h-80 overflow-y-auto">
            <table className="w-full text-left text-xs">
              <tbody className="divide-y divide-border">
                {runList.map((r) => (
                  <tr
                    key={r.run_id}
                    onClick={() => setSelected(r)}
                    className={`cursor-pointer hover:bg-surface-2 ${selected?.run_id === r.run_id ? "bg-surface-2" : ""}`}
                  >
                    <td className="py-1.5 font-mono">{r.run_id}</td>
                    <td>
                      <Badge tone={r.split === "train" ? "muted" : "accent"}>{r.split}</Badge>
                    </td>
                    <td className="font-mono">{num(r.metrics.total_trades, 0)} trades</td>
                    <td className="font-mono">{num(r.metrics.expectancy_r, 3)} R</td>
                    <td className="font-mono">{num(r.metrics.net_return_pct, 2)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>

      {selected && (
        <Card
          title={<span className="font-mono">{selected.run_id}</span>}
          right={
            <Link href={`/chart?runId=${encodeURIComponent(selected.run_id)}`} className="text-xs text-accent underline">
              Trades on chart →
            </Link>
          }
        >
          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            <div>
              <KV k="Strategy" v={`${selected.strategy_name} v${selected.strategy_version}`} />
              <KV k="Split" v={`${selected.split} (${selected.start_date} → ${selected.end_date})`} />
              <KV k="Symbols" v={selected.symbols.join(", ")} />
              <KV k="Created" v={fmtTime(selected.created_at)} />
              <KV k="Engine" v={val(selected.engine_version)} />
              <KV k="Git commit" v={val(selected.git_commit)} />
              <KV k="Agents" v={val(selected.agent_versions)} />
              <KV k="Params" v={val(selected.params)} />
              <KV k="Notes" v={val(selected.notes)} />
              {Number(selected.metrics.total_trades ?? 0) < 100 && (
                <p className="mt-2 text-[11px] text-warn">Fewer than 100 trades: too small a sample to judge an edge.</p>
              )}
            </div>
            <div>
              <Metrics m={selected.metrics} />
            </div>
          </div>
        </Card>
      )}

      <Card title="Compare two runs">
        <div className="flex flex-wrap gap-2 text-xs">
          {(["a", "b"] as const).map((k) => (
            <select
              key={k}
              value={compare[k]}
              onChange={(e) => setCompare({ ...compare, [k]: e.target.value })}
              className="rounded border border-border bg-background px-2 py-1 font-mono"
            >
              <option value="">run {k.toUpperCase()}…</option>
              {runList.map((r) => (
                <option key={r.run_id}>{r.run_id}</option>
              ))}
            </select>
          ))}
          <button disabled={!compare.a || !compare.b} onClick={() => void doCompare()} className="rounded border border-border px-3 py-1 disabled:opacity-40">
            Compare
          </button>
        </div>
        {compared && (
          <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
            {[compared.run_a, compared.run_b].map((r) => (
              <div key={r.run_id} className="rounded bg-surface-2 p-3">
                <div className="mb-1 font-mono text-xs">{r.run_id}</div>
                <Metrics m={r.metrics} />
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
