"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, PageHeader, RefreshButton, fmtAge, useApi, val } from "@/components/ui";

// GET /api/system/status (supervisor + trade service status file + risk_state.json),
// GET /api/live/status (live data health) and GET /api/control/pending (approval gate).
// Unknown values are shown as unknown, never as a healthy default.

export default function OverviewPage() {
  const sys = useApi(api.systemStatus, 5000);
  const live = useApi(api.liveStatus, 5000);
  const pending = useApi(api.listApprovals, 5000);
  const s = sys.data;
  const l = live.data;

  const reload = () => {
    void sys.reload();
    void live.reload();
    void pending.reload();
  };

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader title="Overview" subtitle="Live system state. Anything the backend does not know is shown as unknown.">
        {s && <Badge tone="accent">{s.mode_badge} · no live trading</Badge>}
        <RefreshButton onClick={reload} />
      </PageHeader>
      <ErrorBox error={sys.error || live.error} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card
          title="Risk Manager"
          right={
            s?.risk_state ? <Badge tone={s.risk_state === "normal" ? "up" : "down"}>{s.risk_state}</Badge> : <Badge tone="muted">unknown</Badge>
          }
        >
          {s && !s.risk_state && <Empty>Trade service not running and no risk_state.json: breaker state unknown.</Empty>}
          {s?.risk_state && <KV k="Source" v={s.risk_state_source} />}
          {s?.risk_details && <KV k="Consecutive losses" v={s.risk_details.consecutive_losses} />}
          <Link href="/risk" className="block text-right text-xs text-accent hover:underline">
            Risk page →
          </Link>
        </Card>

        <Card
          title="Market data"
          right={<Badge tone={l?.level === "green" ? "up" : l?.level === "amber" ? "accent" : l ? "down" : "muted"}>{l?.level ?? "unknown"}</Badge>}
        >
          <KV k="Service heartbeat" v={fmtAge(l?.heartbeat_age_s)} />
          <KV k="Clock drift vs Binance" v={l?.clock_drift_ms !== null && l?.clock_drift_ms !== undefined ? `${l.clock_drift_ms} ms` : val(null)} />
          <Link href="/chart" className="block text-right text-xs text-accent hover:underline">
            Chart →
          </Link>
        </Card>

        <Card title="Positions & approvals" right={s && <Badge tone="muted">{s.approval_mode} approval</Badge>}>
          <KV k="Open positions" v={s?.open_positions ? s.open_positions.length : val(null)} hint={s && !s.open_positions ? "trade service not running" : undefined} />
          <KV k="Pending approvals" v={pending.data ? pending.data.length : val(null)} />
          <Link href="/control" className="block text-right text-xs text-accent hover:underline">
            Control →
          </Link>
        </Card>

        <Card title="Trade service stats">
          {s?.stats ? (
            Object.entries(s.stats).map(([k, v]) => <KV key={k} k={k} v={v} />)
          ) : (
            <Empty>No data yet: trade service not running{s?.trade_status_age_s != null ? ` (status file ${Math.round(s.trade_status_age_s)} s old)` : ""}.</Empty>
          )}
          <Link href="/trades" className="block text-right text-xs text-accent hover:underline">
            Journal →
          </Link>
        </Card>
      </div>

      <Card title="Supervised services">
        {s && Object.keys(s.services).length === 0 && <Empty>No services registered with the supervisor.</Empty>}
        {s && Object.keys(s.services).length > 0 && (
          <table className="w-full text-left text-xs">
            <thead className="text-muted">
              <tr>
                {["Service", "Status", "PID", "Uptime", "Restarts", "Last exit code"].map((h) => (
                  <th key={h} className="pb-2 font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {Object.entries(s.services).map(([name, svc]) => (
                <tr key={name}>
                  <td className="py-2 font-semibold">{name}</td>
                  <td>
                    <Badge tone={svc.status === "running" ? "up" : svc.status === "crashing" ? "down" : "muted"}>{svc.status}</Badge>
                  </td>
                  <td className="font-mono">{val(svc.pid)}</td>
                  <td className="font-mono">{svc.status === "running" ? `${svc.uptime_s.toFixed(0)} s` : val(null)}</td>
                  <td className="font-mono">{svc.restart_count}</td>
                  <td className="font-mono">{val(svc.last_exit_code)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <Card title="Alerts">
        {(s?.alerts ?? []).map((a, i) => (
          <div key={`t${i}`} className="rounded border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
            trade service: {a}
          </div>
        ))}
        {(l?.reasons ?? []).map((r, i) => (
          <div key={`l${i}`} className="rounded border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
            market data: {r}
          </div>
        ))}
        {s && l && s.alerts.length === 0 && l.reasons.length === 0 && <Empty>No alerts reported by the trade service or the live data service.</Empty>}
      </Card>
    </div>
  );
}
