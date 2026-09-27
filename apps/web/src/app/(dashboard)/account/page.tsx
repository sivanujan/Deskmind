"use client";

import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, RefreshButton, fmtTime, useApi, val } from "@/components/ui";

// GET /api/dashboard/account: the latest read-only mainnet sync snapshot (data/meta/mainnet_sync_*.json,
// written by `deskmind account sync`). This balance is REAL money, shown for information only.
// The Risk Manager never sizes from it (see the Risk page for the equity it does use).

function Table({ rows }: { rows: Array<Record<string, unknown>> }) {
  if (rows.length === 0) return <Empty>No data yet.</Empty>;
  const cols = Object.keys(rows[0]);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-muted">
          <tr>
            {cols.map((c) => (
              <th key={c} className="px-2 py-1 font-normal">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-border">
              {cols.map((c) => (
                <td key={c} className="px-2 py-1">
                  {val(r[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function AccountPage() {
  const { data, error, loading, reload } = useApi(api.account);

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader
        title="Binance account (mainnet, read-only)"
        subtitle="Real balance from the read-only key. Information only: never used for position sizing, never traded."
      >
        <Badge tone="accent">read-only</Badge>
        <Badge tone="muted">not used for sizing</Badge>
        <RefreshButton onClick={() => void reload()} />
      </PageHeader>
      <ErrorBox error={error} />
      <Loading loading={loading} />

      {data && !data.snapshot && (
        <Card title="No snapshot">
          <Empty>{data.reason}</Empty>
          <p className="text-xs text-muted">
            Key configured: {val(data.key_configured)}. A snapshot is created by <code>uv run deskmind account sync</code>.
          </p>
        </Card>
      )}

      {data?.snapshot && (
        <>
          <Card title="Snapshot" right={<span className="font-mono text-[11px] text-muted">{data.snapshot}</span>}>
            <KV k="Synced at" v={fmtTime(data.synced_at)} />
            {Object.entries(data.permissions ?? {}).map(([k, v]) => (
              <KV key={k} k={k} v={<span className={v && k !== "enableReading" ? "text-down" : ""}>{val(v)}</span>} />
            ))}
          </Card>

          <Card title="USDⓈ-M futures wallet (real money, read-only)">
            {!data.futures_read ? (
              <Empty>{data.futures_reason}</Empty>
            ) : (
              <Table rows={data.balances ?? []} />
            )}
          </Card>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            <Card title="Fee rates: live vs Deskmind config">
              <Table rows={data.comparison?.fee_differences ?? []} />
            </Card>
            <Card title="Maintenance margin (tier 1): live vs config">
              <Table rows={data.comparison?.mmr_differences ?? []} />
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
