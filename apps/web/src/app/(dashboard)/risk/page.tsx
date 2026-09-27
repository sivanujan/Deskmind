"use client";

import { useEffect, useState } from "react";
import { api, type RiskStateInfo } from "@/lib/api";

export default function RiskPage() {
  const [risk, setRisk] = useState<RiskStateInfo | null>(null);
  const [showResetModal, setShowResetModal] = useState(false);
  const [confirmInput, setConfirmInput] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [feedback, setFeedback] = useState<{ message: string; type: "success" | "error" } | null>(null);

  const loadRiskState = async () => {
    try {
      const data = await api.riskState();
      setRisk(data);
    } catch {
      // Ignored
    }
  };

  useEffect(() => {
    let mounted = true;
    const fetchRisk = () => {
      void api.riskState().then((data) => {
        if (mounted) setRisk(data);
      }).catch(() => {});
    };
    fetchRisk();
    const interval = setInterval(fetchRisk, 5000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  const handleResetBreaker = async () => {
    if (confirmInput !== "RESET") return;
    setIsProcessing(true);
    setFeedback(null);
    try {
      await api.resetBreaker("RESET");
      setFeedback({ message: "Circuit breaker successfully reset to NORMAL.", type: "success" });
      setShowResetModal(false);
      setConfirmInput("");
      await loadRiskState();
    } catch (err: unknown) {
      setFeedback({ message: err instanceof Error ? err.message : "Reset failed", type: "error" });
    } finally {
      setIsProcessing(false);
    }
  };

  const ddPct = (risk?.current_drawdown_pct ?? 0) * 100;
  const state = risk?.state ?? "normal";

  return (
    <div className="flex-1 space-y-6 p-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-bold text-foreground">Risk Manager & Circuit Breakers</h1>
          <p className="text-xs text-muted">
            Non-negotiable risk limits, latched drawdown hysteresis, and breaker state surveillance.
          </p>
        </div>

        <div>
          <button
            onClick={() => {
              setShowResetModal(true);
              setConfirmInput("");
            }}
            className="rounded bg-warn/20 border border-warn/40 px-3 py-1.5 text-xs font-semibold text-warn hover:bg-warn/30 transition-colors"
          >
            Reset Breaker State
          </button>
        </div>
      </div>

      {feedback && (
        <div
          className={`flex items-center justify-between rounded-md border p-3 text-xs ${
            feedback.type === "success"
              ? "border-up/40 bg-up/10 text-up"
              : "border-down/40 bg-down/10 text-down"
          }`}
        >
          <span>{feedback.message}</span>
          <button onClick={() => setFeedback(null)}>✕</button>
        </div>
      )}

      {/* State & Drawdown Gauge */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Drawdown Gauge */}
        <div className="rounded-lg border border-border bg-surface p-5 lg:col-span-2 space-y-4">
          <div className="flex items-center justify-between border-b border-border pb-3">
            <div>
              <h2 className="text-sm font-semibold text-foreground">Drawdown & Threshold Surveillance</h2>
              <p className="text-xs text-muted">Continuous tracking of peak-to-trough portfolio equity</p>
            </div>
            <span
              className={`rounded px-2.5 py-1 text-xs font-bold uppercase ${
                state === "normal"
                  ? "bg-up/20 text-up"
                  : state === "half_risk"
                  ? "bg-warn/20 text-warn"
                  : "bg-down/20 text-down"
              }`}
            >
              STATE: {state.replace("_", " ")}
            </span>
          </div>

          <div className="space-y-2">
            <div className="flex justify-between text-xs">
              <span className="text-muted">Current Drawdown:</span>
              <span className="font-mono font-bold text-foreground">{ddPct.toFixed(2)}%</span>
            </div>

            {/* Visual Gauge Bar */}
            <div className="relative h-6 w-full rounded-md bg-surface-2 overflow-hidden border border-border">
              {/* Threshold markers */}
              <div className="absolute top-0 bottom-0 left-[66.6%] w-0.5 bg-warn z-10" title="10% Half-Risk Trigger" />
              <div className="absolute top-0 bottom-0 left-[100%] w-0.5 bg-down z-10" title="15% Halt Trigger" />

              <div
                className={`h-full transition-all duration-500 ${
                  ddPct >= 15 ? "bg-down" : ddPct >= 10 ? "bg-warn" : "bg-up"
                }`}
                style={{ width: `${Math.min(100, (ddPct / 15) * 100)}%` }}
              />
            </div>

            <div className="flex justify-between text-[11px] text-muted">
              <span>0% (Peak)</span>
              <span className="text-warn">10% (Half-Risk Trigger)</span>
              <span className="text-down font-bold">15% (Halt & Close Trigger)</span>
            </div>
          </div>

          {/* Hysteresis Rules */}
          <div className="rounded-md border border-border bg-background p-4 text-xs space-y-1 text-muted">
            <span className="font-semibold text-foreground">Latched Circuit Breaker Rules:</span>
            <div>• <strong className="text-warn">10% Half-Risk:</strong> Reduces position sizing to 50%. Clears only below <strong>7%</strong> hysteresis.</div>
            <div>• <strong className="text-down">15% Drawdown Halt:</strong> Halts all new orders and closes open positions. Clears only below <strong>10%</strong> AND after <strong>7 days</strong>, or via typed manual operator reset.</div>
          </div>
        </div>

        {/* View-Only Risk Limits */}
        <div className="rounded-lg border border-border bg-surface p-5 space-y-3">
          <div className="border-b border-border pb-2">
            <h2 className="text-sm font-semibold text-foreground">Fixed Risk Limits (View-Only)</h2>
            <p className="text-[11px] text-muted">Immutable via UI. Governed by engine configuration.</p>
          </div>

          <div className="space-y-2 text-xs">
            <div className="flex justify-between rounded bg-surface-2 p-2">
              <span className="text-muted">Max Position Notional:</span>
              <span className="font-mono text-foreground">${risk?.limits?.max_position_notional_usdt ?? "50,000"}</span>
            </div>
            <div className="flex justify-between rounded bg-surface-2 p-2">
              <span className="text-muted">Max Account Leverage:</span>
              <span className="font-mono text-foreground">{risk?.limits?.max_leverage ?? 5}x</span>
            </div>
            <div className="flex justify-between rounded bg-surface-2 p-2">
              <span className="text-muted">Daily Loss Limit:</span>
              <span className="font-mono text-foreground">{risk?.limits?.daily_loss_limit_pct ?? 3}%</span>
            </div>
            <div className="flex justify-between rounded bg-surface-2 p-2">
              <span className="text-muted">Max Open Trades:</span>
              <span className="font-mono text-foreground">{risk?.limits?.max_open_trades ?? 3}</span>
            </div>
            <div className="flex justify-between rounded bg-surface-2 p-2">
              <span className="text-muted">ATR Stop Multiplier:</span>
              <span className="font-mono text-foreground">{risk?.limits?.atr_stop_multiplier ?? 2.0}x</span>
            </div>
          </div>
        </div>
      </div>

      {/* Breaker History Timeline */}
      <div className="rounded-lg border border-border bg-surface p-5 space-y-3">
        <h2 className="text-sm font-semibold text-foreground border-b border-border pb-2">
          Circuit Breaker Transition History
        </h2>
        {risk?.breaker_history && risk.breaker_history.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-border text-muted">
                  <th className="pb-2">Timestamp</th>
                  <th className="pb-2">Transition</th>
                  <th className="pb-2">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {risk.breaker_history.map((h, i) => (
                  <tr key={i}>
                    <td className="py-2 font-mono text-muted">{h.timestamp}</td>
                    <td className="py-2 font-bold text-foreground">
                      {h.previous_state} &rarr; {h.new_state}
                    </td>
                    <td className="py-2 text-muted">{h.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="py-6 text-center text-xs text-muted">
            Zero breaker transition events recorded in current cycle.
          </div>
        )}
      </div>

      {/* Reset Breaker Modal */}
      {showResetModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-xs p-4">
          <div className="w-full max-w-md rounded-lg border border-warn/50 bg-surface p-6 shadow-2xl">
            <h2 className="text-base font-bold text-foreground">Manual Circuit Breaker Reset</h2>
            <p className="mt-2 text-xs text-muted">
              Manually unlatches the circuit breaker and returns the system state to NORMAL.
              Type <span className="font-mono text-warn font-bold">RESET</span> to confirm:
            </p>

            <div className="mt-4">
              <input
                type="text"
                value={confirmInput}
                onChange={(e) => setConfirmInput(e.target.value)}
                placeholder="RESET"
                className="w-full rounded border border-border bg-background px-3 py-2 font-mono text-sm text-foreground focus:border-warn focus:outline-hidden"
                autoFocus
              />
            </div>

            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowResetModal(false)}
                className="rounded border border-border px-3 py-1.5 text-xs text-muted hover:text-foreground"
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={confirmInput !== "RESET" || isProcessing}
                onClick={handleResetBreaker}
                className="rounded bg-warn px-4 py-1.5 text-xs font-bold text-black hover:bg-warn/80 disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {isProcessing ? "Resetting..." : "Confirm Reset"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
