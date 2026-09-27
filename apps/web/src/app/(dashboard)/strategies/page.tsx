"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, fmtTime, num, useApi, val } from "@/components/ui";

// GET /api/dashboard/strategies: backtest.runner.STRATEGY_REGISTRY, each class's module docstring
// and default_params, the trend agent config it depends on, and reports/backtests/registry.json.

export default function StrategiesPage() {
  const { data, error, loading } = useApi(api.strategies);

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader title="Strategies" subtitle="Registered strategy classes. Descriptions are the strategy module docstrings." />
      <ErrorBox error={error} />
      <Loading loading={loading} />
      {data && data.length === 0 && <Empty>No strategies registered.</Empty>}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
        {data?.map((s) => {
          const validated = Object.keys(s.runs_by_split).includes("validation");
          return (
            <Card
              key={s.name}
              title={
                <span className="font-mono text-accent">
                  {s.name} <span className="text-xs font-normal text-muted">v{s.version}</span>
                </span>
              }
              right={<Badge tone={validated ? "accent" : "muted"}>{s.validation_status}</Badge>}
            >
              <pre className="whitespace-pre-wrap font-sans text-xs leading-relaxed text-muted">{s.description || "No docstring."}</pre>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                <div className="rounded bg-surface-2 p-3">
                  <div className="mb-1 text-xs font-semibold text-foreground">Strategy parameters</div>
                  {Object.keys(s.params).length === 0 && <Empty>No parameters.</Empty>}
                  {Object.entries(s.params).map(([k, v]) => (
                    <KV key={k} k={k} v={val(v)} />
                  ))}
                  <KV k="timeframes" v={s.timeframes.join(", ") || val(null)} />
                  <KV k="uses agents" v={s.uses_agents.join(", ") || val(null)} />
                </div>
                <div className="rounded bg-surface-2 p-3">
                  <div className="mb-1 text-xs font-semibold text-foreground">Agent parameters used</div>
                  {Object.entries(s.agent_params).map(([k, v]) => (
                    <KV key={k} k={k} v={val(v)} />
                  ))}
                </div>
              </div>

              <div className="rounded bg-surface-2 p-3">
                <div className="mb-1 text-xs font-semibold text-foreground">
                  Backtest runs: {s.runs_total}{" "}
                  <span className="font-normal text-muted">
                    ({Object.entries(s.runs_by_split).map(([k, n]) => `${k} ${n}`).join(", ") || "none"})
                  </span>
                </div>
                {Object.keys(s.latest_by_split).length === 0 && <Empty>No backtest runs yet.</Empty>}
                {Object.entries(s.latest_by_split).map(([split, m]) => (
                  <div key={split} className="mt-2 border-t border-border pt-2">
                    <div className="text-[11px] text-muted">
                      Latest {split} run <span className="font-mono">{String(m.run_id)}</span> · {fmtTime(m.created_at as string)}
                    </div>
                    <KV k="trades" v={val(m.total_trades)} />
                    <KV k="expectancy (R)" v={num(m.expectancy_r, 3)} />
                    <KV k="profit factor" v={num(m.profit_factor, 2)} />
                    <KV k="net return %" v={num(m.net_return_pct, 2)} />
                    <KV k="max drawdown %" v={num(m.max_drawdown_pct, 2)} />
                  </div>
                ))}
              </div>

              <Link
                href="/backtest"
                className="block rounded border border-accent/40 bg-accent/20 py-2 text-center text-xs font-semibold text-accent hover:bg-accent/30"
              >
                Run a backtest &rarr;
              </Link>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
