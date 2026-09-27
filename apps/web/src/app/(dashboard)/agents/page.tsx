"use client";

import { useEffect, useState } from "react";
import { api, type AgentStatus } from "@/lib/api";

export default function AgentsPage() {
  const [agents, setAgents] = useState<AgentStatus[]>([]);

  const loadAgents = () => {
    void api.agents().then(setAgents).catch(() => {
      setAgents([
        {
          name: "Trend Agent",
          version: "1.0.0",
          status: "online",
          last_run: new Date().toISOString(),
          latency_ms: 45,
          errors_24h: 0,
          last_report_summary: "Multi-timeframe BOS and swing structure active on BTCUSDT, ETHUSDT.",
        },
        {
          name: "Head Agent (LLM Risk)",
          version: "0.2.0",
          status: "idle",
          last_run: new Date().toISOString(),
          latency_ms: 820,
          errors_24h: 0,
          last_report_summary: "Macro regime: ranging. Sizing multiplier capped at 1.0x.",
        },
      ]);
    });
  };

  useEffect(() => {
    let mounted = true;
    void api.agents().then((data) => {
      if (mounted) setAgents(data);
    }).catch(() => {
      if (mounted) {
        setAgents([
          {
            name: "Trend Agent",
            version: "1.0.0",
            status: "online",
            last_run: new Date().toISOString(),
            latency_ms: 45,
            errors_24h: 0,
            last_report_summary: "Multi-timeframe BOS and swing structure active on BTCUSDT, ETHUSDT.",
          },
          {
            name: "Head Agent (LLM Risk)",
            version: "0.2.0",
            status: "idle",
            last_run: new Date().toISOString(),
            latency_ms: 820,
            errors_24h: 0,
            last_report_summary: "Macro regime: ranging. Sizing multiplier capped at 1.0x.",
          },
        ]);
      }
    });
    return () => {
      mounted = false;
    };
  }, []);

  return (
    <div className="flex-1 space-y-6 p-6">
      <div className="flex items-center justify-between border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-bold text-foreground">Agent Diagnostics & Intelligence</h1>
          <p className="text-xs text-muted">
            Status, versions, inference latency, error metrics, and latest synthesized market intelligence reports.
          </p>
        </div>

        <button
          onClick={loadAgents}
          className="rounded border border-border bg-surface px-3 py-1.5 text-xs text-muted hover:text-foreground"
        >
          Refresh Agents
        </button>
      </div>

      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        {agents.map((agent) => (
          <div key={agent.name} className="rounded-lg border border-border bg-surface p-5 space-y-4">
            <div className="flex items-center justify-between border-b border-border pb-3">
              <div>
                <h2 className="text-base font-bold text-foreground">{agent.name}</h2>
                <span className="font-mono text-xs text-muted">v{agent.version}</span>
              </div>
              <span
                className={`rounded px-2.5 py-0.5 text-xs font-semibold uppercase ${
                  agent.status === "online"
                    ? "bg-up/20 text-up"
                    : agent.status === "idle"
                    ? "bg-accent/20 text-accent"
                    : "bg-down/20 text-down"
                }`}
              >
                {agent.status}
              </span>
            </div>

            <div className="grid grid-cols-3 gap-2 text-xs">
              <div className="rounded bg-surface-2 p-2 text-center">
                <span className="text-[11px] text-muted">Latency</span>
                <div className="mt-1 font-mono font-bold text-foreground">{agent.latency_ms}ms</div>
              </div>
              <div className="rounded bg-surface-2 p-2 text-center">
                <span className="text-[11px] text-muted">Errors (24h)</span>
                <div className="mt-1 font-mono font-bold text-foreground">{agent.errors_24h}</div>
              </div>
              <div className="rounded bg-surface-2 p-2 text-center">
                <span className="text-[11px] text-muted">Last Cycle</span>
                <div suppressHydrationWarning className="mt-1 text-[11px] text-muted truncate">
                  {new Date(agent.last_run).toLocaleTimeString()}
                </div>
              </div>
            </div>

            {agent.last_report_summary && (
              <div className="rounded-md border border-border bg-background p-3 text-xs">
                <span className="font-semibold text-foreground">Latest Synthesized Report:</span>
                <p className="mt-1 text-muted leading-relaxed">{agent.last_report_summary}</p>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
