"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, PageHeader, fmtTime, useApi, val } from "@/components/ui";

// Service control (supervisor), pending approvals (execution/approval.py ApprovalGate) and the
// emergency stop. Modes are read-only here: they live in .env and apply on trade service restart.

type Feedback = { ok: boolean; msg: string } | null;

export default function ControlPage() {
  const status = useApi(api.systemStatus, 3000);
  const approvals = useApi(api.listApprovals, 3000);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<"KILL" | "STOP_ALL" | null>(null);
  const [confirm, setConfirm] = useState("");
  const [nowS, setNowS] = useState<number | null>(null);

  useEffect(() => {
    const tick = () => setNowS(Date.now() / 1000);
    const t = setInterval(tick, 1000);
    tick();
    return () => clearInterval(t);
  }, []);

  const run = async (fn: () => Promise<unknown>, okMsg: string) => {
    setBusy(true);
    setFeedback(null);
    try {
      await fn();
      setFeedback({ ok: true, msg: okMsg });
    } catch (e) {
      setFeedback({ ok: false, msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
      void status.reload();
      void approvals.reload();
    }
  };

  const s = status.data;

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader title="Control" subtitle="Supervised services, pending trade approvals and the emergency stop.">
        <button
          onClick={() => {
            setModal("KILL");
            setConfirm("");
          }}
          className="rounded-md bg-down px-4 py-2 text-xs font-bold uppercase text-white hover:bg-down/80"
        >
          Emergency stop
        </button>
      </PageHeader>

      <ErrorBox error={status.error} />
      {feedback && (
        <div className={`rounded border p-3 text-xs ${feedback.ok ? "border-up/40 bg-up/10 text-up" : "border-down/40 bg-down/10 text-down"}`}>
          {feedback.msg}
        </div>
      )}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card title="Modes (from .env)">
          {s ? (
            <>
              <KV k="TRADING_MODE" v={s.trading_mode} />
              <KV k="APPROVAL_MODE" v={s.approval_mode} />
              <KV k="Live (mainnet) trading" v={<Badge tone="down">not possible</Badge>} />
              <p className="text-[11px] text-muted">
                Modes are set in <code>.env</code> and take effect when the trade service restarts. The dashboard cannot change them
                (the API answers 409 and records the attempt in the audit log).
              </p>
            </>
          ) : (
            <Empty>No status yet.</Empty>
          )}
        </Card>

        <Card
          title="Services"
          right={
            <button
              onClick={() => {
                setModal("STOP_ALL");
                setConfirm("");
              }}
              className="rounded border border-down/40 bg-down/10 px-2.5 py-1 text-xs text-down hover:bg-down/20"
            >
              Stop all
            </button>
          }
        >
          {!s && <Empty>No status yet.</Empty>}
          {s &&
            Object.entries(s.services).map(([name, svc]) => {
              const running = svc.status === "running";
              return (
                <div key={name} className="flex items-center justify-between rounded-md border border-border bg-surface-2 p-3 text-xs">
                  <div>
                    <div className="font-semibold text-foreground">{name}</div>
                    <div className="text-[11px] text-muted">
                      {svc.status}
                      {svc.pid !== null && ` · pid ${svc.pid}`} · restarts {svc.restart_count}
                      {svc.last_exit_code !== null && ` · last exit ${svc.last_exit_code}`}
                    </div>
                  </div>
                  <div className="flex gap-1.5">
                    {running ? (
                      <>
                        <button
                          disabled={busy}
                          onClick={() => void run(() => api.serviceControl(name as "live", "restart"), `Restart requested for ${name}.`)}
                          className="rounded border border-border bg-background px-2.5 py-1 text-muted hover:text-foreground disabled:opacity-50"
                        >
                          Restart
                        </button>
                        <button
                          disabled={busy}
                          onClick={() => void run(() => api.serviceControl(name as "live", "stop"), `Stop requested for ${name}.`)}
                          className="rounded border border-down/40 bg-down/10 px-2.5 py-1 text-down disabled:opacity-50"
                        >
                          Stop
                        </button>
                      </>
                    ) : (
                      <button
                        disabled={busy}
                        onClick={() => void run(() => api.serviceControl(name as "live", "start"), `Start requested for ${name}.`)}
                        className="rounded border border-up/40 bg-up/10 px-3 py-1 font-semibold text-up disabled:opacity-50"
                      >
                        Start
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
        </Card>
      </div>

      <Card title="Pending trade approvals" right={approvals.data && <Badge tone="muted">{approvals.data.length} pending</Badge>}>
        <ErrorBox error={approvals.error} />
        {approvals.data && approvals.data.length === 0 && (
          <Empty>No approval requests pending (read from the approval gate&apos;s pending file).</Empty>
        )}
        {approvals.data && approvals.data.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-muted">
                <tr>
                  {["Created", "Symbol", "Side", "Strategy", "Entry", "Stop / Target", "Qty", "Notional", "Leverage", "Expires in", ""].map((h) => (
                    <th key={h} className="pb-2 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border font-mono">
                {approvals.data.map((a) => (
                  <tr key={a.id}>
                    <td className="py-2">{fmtTime(a.created_at * 1000)}</td>
                    <td className="py-2 font-bold">{a.intent.symbol}</td>
                    <td className={`py-2 ${a.intent.side.toLowerCase().includes("long") || a.intent.side === "BUY" ? "text-up" : "text-down"}`}>
                      {a.intent.side}
                    </td>
                    <td className="py-2">
                      {a.intent.strategy_name} v{a.intent.strategy_version}
                    </td>
                    <td className="py-2">{a.intent.expected_entry}</td>
                    <td className="py-2">
                      {a.intent.stop_loss} / {a.intent.take_profit}
                    </td>
                    <td className="py-2">{a.decision.qty}</td>
                    <td className="py-2">{a.decision.notional}</td>
                    <td className="py-2">{a.decision.leverage}x</td>
                    <td className="py-2" suppressHydrationWarning>
                      {nowS === null ? "" : `${Math.max(0, Math.round(a.expires_at - nowS))} s`}
                    </td>
                    <td className="space-x-2 py-2 text-right">
                      <button
                        disabled={busy}
                        onClick={() => void run(() => api.decideApproval(a.id, "approve"), `Approved ${a.id}.`)}
                        className="rounded border border-up/40 bg-up/20 px-2.5 py-1 font-semibold text-up disabled:opacity-50"
                      >
                        Approve
                      </button>
                      <button
                        disabled={busy}
                        onClick={() => void run(() => api.decideApproval(a.id, "reject"), `Rejected ${a.id}.`)}
                        className="rounded border border-down/40 bg-down/20 px-2.5 py-1 font-semibold text-down disabled:opacity-50"
                      >
                        Reject
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {s && s.approval_mode === "auto" && <p className="text-[11px] text-muted">APPROVAL_MODE is {val(s.approval_mode)}: requests are only queued in manual mode.</p>}
      </Card>

      {modal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80">
          <div className="w-full max-w-md space-y-4 rounded-lg border border-down/50 bg-surface p-6">
            <h2 className="text-lg font-bold text-down">{modal === "KILL" ? "Emergency stop" : "Stop all services"}</h2>
            <p className="text-xs text-muted">
              Stops every supervised service (live data, trend agent, trade runner). It does not close positions or cancel
              orders: open positions keep their exchange-side stop-loss and take-profit orders. Type{" "}
              <span className="font-mono font-bold text-down">{modal}</span> to confirm.
            </p>
            <input
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder={modal}
              autoFocus
              className="w-full rounded border border-border bg-background px-3 py-2 font-mono text-sm"
            />
            <div className="flex justify-end gap-2">
              <button onClick={() => setModal(null)} className="rounded border border-border px-3 py-1.5 text-xs text-muted">
                Cancel
              </button>
              <button
                disabled={confirm !== modal || busy}
                onClick={() => {
                  const m = modal;
                  setModal(null);
                  void run(
                    () => (m === "KILL" ? api.killSwitch(confirm) : api.stopAllServices(confirm)),
                    "All services stopped.",
                  );
                }}
                className="rounded bg-down px-4 py-1.5 text-xs font-bold text-white disabled:opacity-30"
              >
                Confirm
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
