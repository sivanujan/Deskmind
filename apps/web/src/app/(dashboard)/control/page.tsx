"use client";

import { useEffect, useState } from "react";
import { api, type PendingApproval, type SystemStatus } from "@/lib/api";

export default function ControlPage() {
  const [sysStatus, setSysStatus] = useState<SystemStatus | null>(null);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [feedback, setFeedback] = useState<{ message: string; type: "success" | "error" } | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);

  // Typed confirmation modals
  const [modalType, setModalType] = useState<"KILL" | "STOP_ALL" | null>(null);
  const [confirmInput, setConfirmInput] = useState("");

  const [currentTime, setCurrentTime] = useState(0);

  const refreshData = () => {
    void Promise.allSettled([
      api.systemStatus(),
      api.listApprovals(),
    ]).then(([statusRes, appRes]) => {
      if (statusRes.status === "fulfilled") setSysStatus(statusRes.value);
      if (appRes.status === "fulfilled") setApprovals(appRes.value);
      setCurrentTime(Math.floor(Date.now() / 1000));
    });
  };

  useEffect(() => {
    let mounted = true;
    const fetchControl = () => {
      void Promise.allSettled([
        api.systemStatus(),
        api.listApprovals(),
      ]).then(([statusRes, appRes]) => {
        if (!mounted) return;
        if (statusRes.status === "fulfilled") setSysStatus(statusRes.value);
        if (appRes.status === "fulfilled") setApprovals(appRes.value);
        setCurrentTime(Math.floor(Date.now() / 1000));
      });
    };
    fetchControl();
    const interval = setInterval(fetchControl, 3000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  const handleServiceAction = async (service: "live" | "trend" | "trade", action: "start" | "stop" | "restart") => {
    setIsProcessing(true);
    setFeedback(null);
    try {
      await api.serviceControl(service, action);
      setFeedback({ message: `Successfully requested ${action} for ${service} daemon.`, type: "success" });
      await refreshData();
    } catch (err: unknown) {
      setFeedback({ message: err instanceof Error ? err.message : `Failed to ${action} ${service}`, type: "error" });
    } finally {
      setIsProcessing(false);
    }
  };

  const handleModeChange = async (mode: "paper" | "testnet", approval: "auto" | "manual") => {
    setIsProcessing(true);
    setFeedback(null);
    try {
      await api.setMode(mode, approval);
      setFeedback({ message: `Mode updated to ${mode.toUpperCase()} (${approval.toUpperCase()} approval)`, type: "success" });
      await refreshData();
    } catch (err: unknown) {
      setFeedback({ message: err instanceof Error ? err.message : "Failed to change mode", type: "error" });
    } finally {
      setIsProcessing(false);
    }
  };

  const handleApproval = async (intentId: string, action: "approve" | "reject") => {
    setIsProcessing(true);
    setFeedback(null);
    try {
      await api.decideApproval(intentId, action);
      setFeedback({ message: `Trade intent ${intentId} ${action}d.`, type: "success" });
      await refreshData();
    } catch (err: unknown) {
      setFeedback({ message: err instanceof Error ? err.message : `Failed to ${action} approval`, type: "error" });
    } finally {
      setIsProcessing(false);
    }
  };

  const executeTypedAction = async () => {
    if (!modalType) return;
    setIsProcessing(true);
    setFeedback(null);
    try {
      if (modalType === "KILL") {
        await api.killSwitch(confirmInput);
        setFeedback({ message: "EMERGENCY KILL SWITCH TRIGGERED: all child processes halted.", type: "success" });
      } else if (modalType === "STOP_ALL") {
        await api.stopAllServices(confirmInput);
        setFeedback({ message: "All services stopped successfully.", type: "success" });
      }
      setModalType(null);
      setConfirmInput("");
      await refreshData();
    } catch (err: unknown) {
      setFeedback({ message: err instanceof Error ? err.message : "Confirmation failed", type: "error" });
    } finally {
      setIsProcessing(false);
    }
  };

  const services = sysStatus?.services ?? {};
  const currentMode = sysStatus?.trading_mode ?? "testnet";
  const currentApproval = sysStatus?.approval_mode ?? "auto";

  return (
    <div className="flex-1 space-y-6 p-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-bold text-foreground">Operational Control Hub</h1>
          <p className="text-xs text-muted">
            Manage child daemons, execution modes, pending trade authorizations, and emergency circuit controls.
          </p>
        </div>

        {/* KILL SWITCH BUTTON */}
        <div>
          <button
            onClick={() => {
              setModalType("KILL");
              setConfirmInput("");
            }}
            className="flex items-center gap-2 rounded-md bg-down px-4 py-2 text-xs font-bold uppercase tracking-wider text-white shadow-lg hover:bg-down/80 transition-transform active:scale-95"
          >
            <span className="h-2 w-2 rounded-full bg-white animate-ping" />
            EMERGENCY KILL SWITCH
          </button>
        </div>
      </div>

      {/* Feedback Banner */}
      {feedback && (
        <div
          className={`flex items-center justify-between rounded-md border p-3 text-xs ${
            feedback.type === "success"
              ? "border-up/40 bg-up/10 text-up"
              : "border-down/40 bg-down/10 text-down"
          }`}
        >
          <span>{feedback.message}</span>
          <button onClick={() => setFeedback(null)} className="font-bold hover:underline">
            ✕
          </button>
        </div>
      )}

      {/* Two Column Section: Modes & Service Lifecycle */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Left: Trading Mode & Approval Mode */}
        <div className="rounded-lg border border-border bg-surface p-5 space-y-4">
          <div className="border-b border-border pb-3">
            <h2 className="text-sm font-semibold text-foreground">Execution & Authorization Modes</h2>
            <p className="text-xs text-muted">Switch between simulated Paper and Binance USDT-M Demo/Testnet</p>
          </div>

          <div>
            <label className="block text-xs font-medium text-muted">Active Trading Mode</label>
            <div className="mt-2 flex gap-3">
              <button
                type="button"
                onClick={() => handleModeChange("paper", currentApproval)}
                className={`flex-1 rounded-md border p-3 text-left transition-colors ${
                  currentMode === "paper"
                    ? "border-accent bg-accent/15 text-accent font-semibold"
                    : "border-border bg-background text-muted hover:text-foreground"
                }`}
              >
                <div className="text-sm font-bold">PAPER</div>
                <div className="mt-0.5 text-xs">Zero exchange latency. Simulated book & fills.</div>
              </button>

              <button
                type="button"
                onClick={() => handleModeChange("testnet", currentApproval)}
                className={`flex-1 rounded-md border p-3 text-left transition-colors ${
                  currentMode === "testnet"
                    ? "border-warn bg-warn/15 text-warn font-semibold"
                    : "border-border bg-background text-muted hover:text-foreground"
                }`}
              >
                <div className="text-sm font-bold">DEMO / TESTNET</div>
                <div className="mt-0.5 text-xs">Binance USDT-M Futures testnet orders.</div>
              </button>
            </div>
            <p className="mt-2 text-[11px] text-muted italic">
              Note: Live Mainnet execution is strictly forbidden and permanently rejected by the backend.
            </p>
          </div>

          <div className="border-t border-border pt-4">
            <label className="block text-xs font-medium text-muted">Trade Approval Pipeline</label>
            <div className="mt-2 flex gap-3">
              <button
                type="button"
                onClick={() => handleModeChange(currentMode, "auto")}
                className={`flex-1 rounded-md border p-2.5 text-center text-xs font-medium transition-colors ${
                  currentApproval === "auto"
                    ? "border-accent bg-accent/15 text-accent font-bold"
                    : "border-border bg-background text-muted hover:text-foreground"
                }`}
              >
                AUTOMATIC APPROVAL
              </button>
              <button
                type="button"
                onClick={() => handleModeChange(currentMode, "manual")}
                className={`flex-1 rounded-md border p-2.5 text-center text-xs font-medium transition-colors ${
                  currentApproval === "manual"
                    ? "border-accent bg-accent/15 text-accent font-bold"
                    : "border-border bg-background text-muted hover:text-foreground"
                }`}
              >
                MANUAL OPERATOR CONFIRM
              </button>
            </div>
          </div>
        </div>

        {/* Right: Supervisor Child Services Control */}
        <div className="rounded-lg border border-border bg-surface p-5 space-y-4">
          <div className="flex items-center justify-between border-b border-border pb-3">
            <div>
              <h2 className="text-sm font-semibold text-foreground">Process Supervisor Control</h2>
              <p className="text-xs text-muted">Start, stop, and restart background daemon processes</p>
            </div>
            <button
              onClick={() => {
                setModalType("STOP_ALL");
                setConfirmInput("");
              }}
              className="rounded border border-down/40 bg-down/10 px-2.5 py-1 text-xs font-medium text-down hover:bg-down/20"
            >
              Stop All Daemons
            </button>
          </div>

          <div className="space-y-3">
            {(["live", "trend", "trade"] as const).map((svcKey) => {
              const svc = services[svcKey];
              const isRunning = svc?.status === "running";
              return (
                <div
                  key={svcKey}
                  className="flex items-center justify-between rounded-md border border-border bg-surface-2 p-3 text-xs"
                >
                  <div>
                    <div className="font-semibold capitalize text-foreground">{svcKey} Service</div>
                    <div className="text-[11px] text-muted">
                      Status: <span className={isRunning ? "text-up font-bold" : "text-muted"}>{svc?.status ?? "stopped"}</span>
                      {svc?.pid && ` · PID: ${svc.pid}`} · Restarts: {svc?.restart_count ?? 0}
                    </div>
                  </div>

                  <div className="flex items-center gap-1.5">
                    {isRunning ? (
                      <>
                        <button
                          disabled={isProcessing}
                          onClick={() => handleServiceAction(svcKey, "restart")}
                          className="rounded border border-border bg-background px-2.5 py-1 text-xs text-muted hover:text-foreground disabled:opacity-50"
                        >
                          Restart
                        </button>
                        <button
                          disabled={isProcessing}
                          onClick={() => handleServiceAction(svcKey, "stop")}
                          className="rounded border border-down/40 bg-down/10 px-2.5 py-1 text-xs text-down hover:bg-down/20 disabled:opacity-50"
                        >
                          Stop
                        </button>
                      </>
                    ) : (
                      <button
                        disabled={isProcessing}
                        onClick={() => handleServiceAction(svcKey, "start")}
                        className="rounded border border-up/40 bg-up/10 px-3 py-1 text-xs font-semibold text-up hover:bg-up/20 disabled:opacity-50"
                      >
                        Start
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {/* Pending Approvals Table */}
      <div className="rounded-lg border border-border bg-surface p-5 space-y-3">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div>
            <h2 className="text-sm font-semibold text-foreground">Pending Trade Approvals</h2>
            <p className="text-xs text-muted">
              Signals awaiting manual risk confirmation before exchange submission
            </p>
          </div>
          <span className="rounded bg-surface-2 px-2 py-0.5 text-xs text-muted">
            {approvals.length} Pending
          </span>
        </div>

        {approvals.length === 0 ? (
          <div className="py-6 text-center text-xs text-muted">
            No trade authorizations pending. When in MANUAL approval mode, new strategy setups will appear here.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border text-muted">
                  <th className="pb-2">Intent ID</th>
                  <th className="pb-2">Symbol</th>
                  <th className="pb-2">Side</th>
                  <th className="pb-2">Qty</th>
                  <th className="pb-2">Entry Price</th>
                  <th className="pb-2">SL / TP</th>
                  <th className="pb-2">Risk Decision</th>
                  <th className="pb-2">Expiry</th>
                  <th className="pb-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {approvals.map((app) => {
                  const remainingSec = currentTime ? Math.max(0, Math.floor(app.expires_at - currentTime)) : 0;
                  return (
                    <tr key={app.intent_id} className="hover:bg-surface-2/40">
                      <td className="py-2.5 font-mono text-muted">{app.intent_id.slice(0, 10)}...</td>
                      <td className="py-2.5 font-bold text-foreground">{app.symbol}</td>
                      <td className="py-2.5">
                        <span
                          className={`rounded px-1.5 py-0.5 font-bold ${
                            app.direction === "LONG" ? "bg-up/20 text-up" : "bg-down/20 text-down"
                          }`}
                        >
                          {app.direction}
                        </span>
                      </td>
                      <td className="py-2.5 font-mono">{app.quantity}</td>
                      <td className="py-2.5 font-mono">${app.entry_price.toFixed(2)}</td>
                      <td className="py-2.5 font-mono text-muted">
                        ${app.stop_loss.toFixed(2)} / ${app.take_profit.toFixed(2)}
                      </td>
                      <td className="py-2.5">
                        <span className="text-accent font-medium">{app.risk_decision.status}</span>
                        {app.risk_decision.approved_fraction && (
                          <span className="ml-1 text-muted text-[11px]">
                            ({(app.risk_decision.approved_fraction * 100).toFixed(0)}%)
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 font-mono text-warn">{remainingSec}s</td>
                      <td className="py-2.5 text-right space-x-2">
                        <button
                          disabled={isProcessing}
                          onClick={() => handleApproval(app.intent_id, "approve")}
                          className="rounded bg-up/20 border border-up/40 px-2.5 py-1 text-xs font-semibold text-up hover:bg-up/30 disabled:opacity-50"
                        >
                          Approve
                        </button>
                        <button
                          disabled={isProcessing}
                          onClick={() => handleApproval(app.intent_id, "reject")}
                          className="rounded bg-down/20 border border-down/40 px-2.5 py-1 text-xs font-semibold text-down hover:bg-down/30 disabled:opacity-50"
                        >
                          Reject
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* TYPED CONFIRMATION MODAL */}
      {modalType && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-xs">
          <div className="w-full max-w-md rounded-lg border border-down/50 bg-surface p-6 shadow-2xl">
            <div className="flex items-center gap-3 text-down">
              <span className="text-2xl">⚠️</span>
              <h2 className="text-lg font-bold">
                {modalType === "KILL" ? "EMERGENCY KILL SWITCH" : "STOP ALL DAEMONS"}
              </h2>
            </div>

            <p className="mt-2 text-xs text-muted">
              {modalType === "KILL"
                ? "This will unconditionally terminate all child processes and trip the circuit breaker into halt_and_close state. To proceed, type 'KILL' below."
                : "This will terminate all running services (market data, trend agent, trade runner). To proceed, type 'STOP_ALL' below."}
            </p>

            <div className="mt-4">
              <label className="block text-xs font-semibold text-foreground">
                Type <span className="font-mono text-down font-bold">{modalType}</span> to confirm:
              </label>
              <input
                type="text"
                value={confirmInput}
                onChange={(e) => setConfirmInput(e.target.value)}
                placeholder={modalType}
                className="mt-1 w-full rounded border border-border bg-background px-3 py-2 font-mono text-sm text-foreground focus:border-down focus:outline-hidden"
                autoFocus
              />
            </div>

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => {
                  setModalType(null);
                  setConfirmInput("");
                }}
                className="rounded border border-border px-3 py-1.5 text-xs text-muted hover:text-foreground"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={confirmInput !== modalType || isProcessing}
                onClick={executeTypedAction}
                className="rounded bg-down px-4 py-1.5 text-xs font-bold text-white hover:bg-down/80 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {isProcessing ? "Executing..." : "Confirm & Execute"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
