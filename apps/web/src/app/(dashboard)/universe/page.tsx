"use client";

import TaskButton from "@/components/TaskButton";
import { api, type UniverseRunSummary } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, RefreshButton, fmtTime, useApi, val } from "@/components/ui";

// GET /api/universe: universe_symbols (lifecycle state per symbol) and universe_runs (daily
// summaries), written by the daily job (`deskmind universe run` / the supervisor's "universe").

const TONE: Record<string, "up" | "down" | "accent" | "muted"> = {
  tradeable: "up",
  watch_only: "accent",
  downloading: "muted",
  verifying: "muted",
  warming_up: "muted",
  blocked: "down",
  exited: "muted",
};

function gb(bytes: number) {
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

function list(items: string[] | undefined) {
  return items && items.length > 0 ? items.join(", ") : "none";
}

function Summary({ run }: { run: UniverseRunSummary }) {
  return (
    <div className="space-y-1">
      <KV k="Run" v={<span>{run.run_date} · {fmtTime(run.started_at)}</span>} />
      <KV k="Members" v={val(run.members)} />
      <KV k="Entered" v={list(run.entered)} />
      <KV k="Exited" v={list(run.exited)} />
      <KV k="New watch-only listings" v={list(run.new_watch_only)} />
      <KV k="Promoted to tradeable" v={list(run.promoted)} />
      <KV k="Downloads done" v={list(run.downloads_done)} />
      <KV
        k="Downloads failed"
        v={run.downloads_failed.length ? run.downloads_failed.map((d) => `${d.symbol} (${d.error})`).join("; ") : "none"}
      />
      <KV
        k="Verification failed"
        v={run.verify_failed.length ? run.verify_failed.map((d) => d.symbol).join(", ") : "none"}
      />
      <KV
        k="Disk"
        v={run.disk ? `${run.disk.used_pct}% used · parquet ${gb(run.disk.data_dir_bytes)} · free ${gb(run.disk.free_bytes)}` : val(null)}
      />
      {run.alerts.map((a, i) => (
        <div key={i} className="rounded border border-down/40 bg-down/10 px-2 py-1 text-xs text-down">
          {a}
        </div>
      ))}
    </div>
  );
}

export default function UniversePage() {
  const { data, error, loading, reload } = useApi(api.universe, 15000);
  const last = data?.runs[0];

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader
        title="Universe"
        subtitle="Liquidity-only symbol universe and each symbol's lifecycle. Trading needs state 'tradeable' AND the symbol in TRADE_SYMBOLS."
      >
        <TaskButton
          kind="universe_run"
          label="Run universe job now"
          start={api.runUniverseJob}
          confirmText="Run the daily universe job now? It ranks symbols and may download, verify and warm up new ones (can take a while)."
          onDone={() => void reload()}
        />
        <RefreshButton onClick={() => void reload()} />
      </PageHeader>
      <ErrorBox error={error} />
      <Loading loading={loading} />
      {data?.reason && <Empty>{data.reason}. Click &quot;Run universe job now&quot; (log in first), start the &quot;universe&quot; service on Control, or run `uv run deskmind universe run`.</Empty>}

      {data && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <Card title="Latest daily summary">{last ? <Summary run={last} /> : <Empty>No daily run yet.</Empty>}</Card>
          <Card title="Rules (from config)">
            {Object.entries(data.config).map(([k, v]) => (
              <KV key={k} k={k} v={String(v)} />
            ))}
            <KV k="TRADE_SYMBOLS" v={data.trade_symbols.join(", ")} />
          </Card>
          <Card title="By state">
            {last?.by_state ? (
              Object.entries(last.by_state).map(([k, n]) => <KV key={k} k={<Badge tone={TONE[k] ?? "muted"}>{k}</Badge>} v={n} />)
            ) : (
              <Empty>No data yet.</Empty>
            )}
          </Card>
        </div>
      )}

      {data && data.symbols.length > 0 && (
        <Card title={`Symbols (${data.symbols.length})`}>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-muted">
                <tr>
                  {["Rank", "Symbol", "State", "Median daily volume (USDT)", "Listed", "Verified", "Warmed up", "Trade allowed", "Reason / last change"].map((h) => (
                    <th key={h} className="pb-2 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border font-mono">
                {data.symbols.map((s) => {
                  const canTrade = s.state === "tradeable" && data.trade_symbols.includes(s.symbol);
                  return (
                    <tr key={s.symbol}>
                      <td className="py-1.5">{val(s.rank)}</td>
                      <td className="font-bold">{s.symbol}</td>
                      <td>
                        <Badge tone={TONE[s.state] ?? "muted"}>{s.state}</Badge>
                      </td>
                      <td>{s.median_quote_volume === null ? val(null) : Math.round(s.median_quote_volume).toLocaleString("en-US")}</td>
                      <td>{s.onboard_ms ? fmtTime(s.onboard_ms) : val(null)}</td>
                      <td>{val(s.data_verified)}</td>
                      <td>{val(s.warmed_up)}</td>
                      <td className={canTrade ? "text-up" : "text-muted"}>{canTrade ? "yes" : "no"}</td>
                      <td className="max-w-md truncate font-sans" title={s.reason ?? ""}>
                        {s.reason ?? ""} {s.state_changed_at_ms ? <span className="text-muted">({fmtTime(s.state_changed_at_ms)})</span> : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {data && data.runs.length > 1 && (
        <Card title="Earlier daily runs">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {data.runs.slice(1).map((r) => (
              <div key={r.started_at} className="rounded bg-surface-2 p-3">
                <Summary run={r} />
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
