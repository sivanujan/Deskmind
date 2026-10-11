"use client";

import { api, type UniverseSummary } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, RefreshButton, fmtTime, useApi, val } from "@/components/ui";

// GET /api/dashboard/universe: lifecycle state per symbol (universe_symbols) and the daily run
// summaries (universe_runs), written by the 00:00 UTC job (`deskmind universe run`).

function stateTone(state: string): "up" | "down" | "accent" | "muted" {
  if (state === "tradeable") return "up";
  if (state === "blocked") return "down";
  if (state === "exited") return "muted";
  return "accent";
}

function List({ items }: { items: string[] }) {
  if (items.length === 0) return <span className="text-muted">none</span>;
  return <span className="font-mono">{items.join(", ")}</span>;
}

function Summary({ s }: { s: UniverseSummary }) {
  return (
    <div className="space-y-1 text-xs">
      <KV k="Status" v={s.status} />
      <KV k="Universe size" v={val(s.universe_size)} />
      <KV k="Tradeable" v={<List items={s.tradeable} />} />
      <KV k="Entered" v={<List items={s.entered.map((e) => e.symbol)} />} />
      <KV k="Exited" v={<List items={s.exited} />} />
      <KV k="New watch_only listings" v={<List items={s.new_watch_only} />} />
      <KV k="Promoted from watch_only" v={<List items={s.promoted} />} />
      <KV k="Downloads done" v={<List items={s.downloads.done.map((d) => `${d.symbol} (${d.kind})`)} />} />
      <KV k="Downloads failed" v={<List items={s.downloads.failed.map((d) => `${d.symbol}: ${d.error}`)} />} />
      <KV k="Verification failed" v={<List items={s.verification.failed.map((d) => d.symbol)} />} />
      <KV k="Warm-up failed" v={<List items={s.warm_up.failed.map((d) => d.symbol)} />} />
      <KV k="Skipped (disk guard)" v={<List items={s.skipped_disk_guard} />} />
      <KV
        k="Disk usage"
        v={
          s.disk.used_pct === undefined
            ? val(null)
            : `${s.disk.used_pct}% used (guard at ${s.disk.threshold_pct}%), parquet ${((s.disk.data_dir_bytes ?? 0) / 1e9).toFixed(2)} GB`
        }
      />
      <KV k="Disk guard tripped" v={val(s.disk.guard_tripped)} />
      {s.alerts.length > 0 && (
        <div className="mt-2 space-y-1">
          {s.alerts.map((a, i) => (
            <div key={i} className="rounded border border-down/40 bg-down/10 px-2 py-1 text-down">
              {a.kind}
              {a.symbol ? ` ${a.symbol}` : ""}: {a.message}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function UniversePage() {
  const { data, error, loading, reload } = useApi(api.universe, 30000);
  const latest = data?.runs?.[0];

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader
        title="Trading universe"
        subtitle="Liquidity-ranked USDT-M perpetuals. Recomputed daily at 00:00 UTC; a symbol trades only after verified data and agent warm-up."
      >
        <RefreshButton onClick={() => void reload()} />
      </PageHeader>
      <ErrorBox error={error} />
      <Loading loading={loading} />

      {data && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <Card title="Schedule & thresholds">
            <KV k="Next run" v={fmtTime(data.next_run_at)} />
            {Object.entries(data.config).map(([k, v]) => (
              <KV key={k} k={k} v={val(v)} />
            ))}
          </Card>
          <Card title={latest ? `Daily summary ${latest.run_date}` : "Daily summary"}>
            {latest?.summary ? <Summary s={latest.summary} /> : <Empty>{data.reason ?? "No data yet."}</Empty>}
          </Card>
        </div>
      )}

      {data && (
        <Card title="Symbols">
          {data.symbols === null ? (
            <Empty>{data.reason}</Empty>
          ) : data.symbols.length === 0 ? (
            <Empty>No data yet.</Empty>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="text-muted">
                  <tr>
                    {["symbol", "state", "rank", "24h quote vol", "listed", "verified", "warmed up", "blocked", "last error"].map((c) => (
                      <th key={c} className="px-2 py-1 font-normal">
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {data.symbols.map((r) => (
                    <tr key={r.symbol} className="border-t border-border">
                      <td className="px-2 py-1">{r.symbol}</td>
                      <td className="px-2 py-1">
                        <Badge tone={stateTone(r.state)}>{r.state}</Badge>
                      </td>
                      <td className="px-2 py-1">{val(r.rank)}</td>
                      <td className="px-2 py-1">{r.quote_volume_24h === null ? val(null) : Number(r.quote_volume_24h).toLocaleString("en-US", { maximumFractionDigits: 0 })}</td>
                      <td className="px-2 py-1">{fmtTime(r.onboard_at)}</td>
                      <td className="px-2 py-1">{fmtTime(r.verified_at)}</td>
                      <td className="px-2 py-1">{fmtTime(r.warmed_up_at)}</td>
                      <td className="px-2 py-1">{val(r.blocked_reason)}</td>
                      <td className="px-2 py-1">{val(r.last_error)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
