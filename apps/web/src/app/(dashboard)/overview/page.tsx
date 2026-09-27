"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, type SystemStatus, type LiveStatus } from "@/lib/api";

export default function OverviewPage() {
  const [sysStatus, setSysStatus] = useState<SystemStatus | null>(null);
  const [liveStatus, setLiveStatus] = useState<LiveStatus | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);

  const fetchStatus = () => {
    void Promise.allSettled([api.systemStatus(), api.liveStatus()]).then(([sys, live]) => {
      if (sys.status === "fulfilled") setSysStatus(sys.value);
      if (live.status === "fulfilled") setLiveStatus(live.value);
      setLastRefreshed(new Date());
    });
  };

  useEffect(() => {
    let mounted = true;
    const run = () => {
      void Promise.allSettled([api.systemStatus(), api.liveStatus()]).then(([sys, live]) => {
        if (!mounted) return;
        if (sys.status === "fulfilled") setSysStatus(sys.value);
        if (live.status === "fulfilled") setLiveStatus(live.value);
        setLastRefreshed(new Date());
      });
    };
    run();
    const interval = setInterval(run, 5000);
    return () => {
      mounted = false;
      clearInterval(interval);
    };
  }, []);

  const tradingMode = sysStatus?.trading_mode ?? "testnet";
  const approvalMode = sysStatus?.approval_mode ?? "auto";
  const riskState = sysStatus?.risk_state ?? "normal";
  const services = sysStatus?.services ?? {};

  return (
    <div className="flex-1 space-y-6 p-6">
      {/* Top Banner: Hard Safety Badge & System State */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-surface p-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-foreground">Mission Control Overview</h1>
          <p className="text-xs text-muted">
            Continuous real-time surveillance of executors, agents, data freshness, and risk limits.
          </p>
        </div>

        <div className="flex items-center gap-3">
          {/* HARD SAFETY BADGE */}
          <div className="flex items-center gap-2 rounded-md border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-bold text-accent">
            <span className="h-2 w-2 rounded-full bg-accent animate-pulse" />
            <span>OPERATING MODE: {tradingMode.toUpperCase()} (NO LIVE TRADING ALLOWED)</span>
          </div>

          <button
            onClick={fetchStatus}
            suppressHydrationWarning
            className="rounded border border-border bg-surface-2 px-3 py-1.5 text-xs font-medium text-muted hover:text-foreground transition-colors"
          >
            Refresh{lastRefreshed ? ` (${lastRefreshed.toLocaleTimeString()})` : ""}
          </button>
        </div>
      </div>

      {/* Grid: 4 Metric Cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* Card 1: Risk State */}
        <div className="rounded-lg border border-border bg-surface p-4">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted">Risk Manager State</span>
            <span
              className={`rounded px-2 py-0.5 text-xs font-bold ${
                riskState === "normal"
                  ? "bg-up/20 text-up"
                  : riskState === "half_risk"
                  ? "bg-warn/20 text-warn"
                  : "bg-down/20 text-down"
              }`}
            >
              {riskState.replace("_", " ").toUpperCase()}
            </span>
          </div>
          <div className="mt-3">
            <div className="text-2xl font-bold text-foreground">
              {sysStatus?.circuit_breaker_tripped ? "TRIPPED" : "HEALTHY"}
            </div>
            <p className="mt-1 text-xs text-muted">
              {sysStatus?.circuit_breaker_tripped
                ? "Circuit breaker latched: halt & close orders"
                : "All circuit breakers within thresholds"}
            </p>
          </div>
          <div className="mt-3 border-t border-border pt-2 text-right">
            <Link href="/risk" className="text-xs font-medium text-accent hover:underline">
              Inspect Risk &rarr;
            </Link>
          </div>
        </div>

        {/* Card 2: Data Freshness */}
        <div className="rounded-lg border border-border bg-surface p-4">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted">Data Freshness</span>
            <span
              className={`rounded px-2 py-0.5 text-xs font-bold ${
                liveStatus?.level === "green"
                  ? "bg-up/20 text-up"
                  : liveStatus?.level === "amber"
                  ? "bg-warn/20 text-warn"
                  : "bg-down/20 text-down"
              }`}
            >
              {(liveStatus?.level ?? "UNKNOWN").toUpperCase()}
            </span>
          </div>
          <div className="mt-3">
            <div className="text-2xl font-bold text-foreground">
              {liveStatus?.clock_drift_ms !== null && liveStatus?.clock_drift_ms !== undefined
                ? `${liveStatus.clock_drift_ms} ms`
                : "Synced"}
            </div>
            <p className="mt-1 text-xs text-muted">
              Clock drift vs Binance server. Heartbeat age: {liveStatus?.heartbeat_age_s?.toFixed(1) ?? "0.0"}s
            </p>
          </div>
          <div className="mt-3 border-t border-border pt-2 text-right">
            <Link href="/chart" className="text-xs font-medium text-accent hover:underline">
              Open Charts &rarr;
            </Link>
          </div>
        </div>

        {/* Card 3: Execution & Approvals */}
        <div className="rounded-lg border border-border bg-surface p-4">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted">Approval Pipeline</span>
            <span className="rounded bg-surface-2 px-2 py-0.5 text-xs font-medium text-muted uppercase">
              {approvalMode}
            </span>
          </div>
          <div className="mt-3">
            <div className="text-2xl font-bold text-foreground">
              {sysStatus?.open_positions_count ?? 0}
            </div>
            <p className="mt-1 text-xs text-muted">Active open positions in current session</p>
          </div>
          <div className="mt-3 border-t border-border pt-2 text-right">
            <Link href="/control" className="text-xs font-medium text-accent hover:underline">
              Pending Approvals &rarr;
            </Link>
          </div>
        </div>

        {/* Card 4: Daily Activity */}
        <div className="rounded-lg border border-border bg-surface p-4">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted">Today&apos;s Order Activity</span>
            <span className="text-xs text-muted">Session</span>
          </div>
          <div className="mt-3">
            <div className="text-2xl font-bold text-foreground">0 Pending</div>
            <p className="mt-1 text-xs text-muted">Intents generated: 0 | Orders placed: 0</p>
          </div>
          <div className="mt-3 border-t border-border pt-2 text-right">
            <Link href="/trades" className="text-xs font-medium text-accent hover:underline">
              Trade Journal &rarr;
            </Link>
          </div>
        </div>
      </div>

      {/* Services Health Table */}
      <div className="rounded-lg border border-border bg-surface p-5">
        <div className="flex items-center justify-between border-b border-border pb-3">
          <div>
            <h2 className="text-sm font-semibold text-foreground">Child Services Supervision</h2>
            <p className="text-xs text-muted">Processes managed by local supervisor with exponential backoff</p>
          </div>
          <Link
            href="/control"
            className="rounded bg-accent/20 border border-accent/40 px-3 py-1 text-xs font-medium text-accent hover:bg-accent/30"
          >
            Manage Services
          </Link>
        </div>

        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-xs">
            <thead>
              <tr className="border-b border-border text-muted">
                <th className="pb-2 font-medium">Service Name</th>
                <th className="pb-2 font-medium">Process Status</th>
                <th className="pb-2 font-medium">PID</th>
                <th className="pb-2 font-medium">Uptime</th>
                <th className="pb-2 font-medium">Restarts</th>
                <th className="pb-2 font-medium">Last Diagnostic / Error</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {Object.keys(services).length > 0 ? (
                Object.entries(services).map(([key, svc]) => (
                  <tr key={key} className="hover:bg-surface-2/40">
                    <td className="py-2.5 font-semibold text-foreground capitalize">{key} Daemon</td>
                    <td className="py-2.5">
                      <span
                        className={`inline-flex items-center gap-1 rounded px-2 py-0.5 font-medium ${
                          svc.status === "running"
                            ? "bg-up/20 text-up"
                            : svc.status === "crashing"
                            ? "bg-down/20 text-down"
                            : "bg-muted/20 text-muted"
                        }`}
                      >
                        <span
                          className={`h-1.5 w-1.5 rounded-full ${
                            svc.status === "running" ? "bg-up" : "bg-muted"
                          }`}
                        />
                        {svc.status.toUpperCase()}
                      </span>
                    </td>
                    <td className="py-2.5 font-mono text-muted">{svc.pid ?? "—"}</td>
                    <td className="py-2.5 text-muted">{svc.uptime_seconds.toFixed(0)}s</td>
                    <td className="py-2.5 text-muted">{svc.restart_count}</td>
                    <td className="py-2.5 font-mono text-xs text-muted">
                      {svc.last_error ?? "Operational / Normal"}
                    </td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={6} className="py-4 text-center text-muted">
                    Supervisor offline. Start services via Control panel or run:{" "}
                    <code className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-accent">
                      uv run deskmind supervisor
                    </code>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Active Alerts Panel */}
      <div className="rounded-lg border border-border bg-surface p-5">
        <h2 className="text-sm font-semibold text-foreground">Surveillance Alerts & Log Feed</h2>
        <div className="mt-3 space-y-2">
          {liveStatus?.reasons && liveStatus.reasons.length > 0 ? (
            liveStatus.reasons.map((r, i) => (
              <div
                key={i}
                className="flex items-center justify-between rounded border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn"
              >
                <span>⚠️ {r}</span>
                <span className="font-mono text-xs text-muted">Freshness Monitor</span>
              </div>
            ))
          ) : (
            <div className="flex items-center gap-2 rounded border border-up/30 bg-up/5 px-3 py-2 text-xs text-up">
              <span>✓ All surveillance feeds normal. Zero active warnings.</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
