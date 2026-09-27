"use client";

import { api } from "@/lib/api";
import { Badge, Card, Empty, ErrorBox, KV, Loading, PageHeader, RefreshButton, fmtAge, fmtTime, useApi, val } from "@/components/ui";

// Everything on this page comes from GET /api/dashboard/agents, which reads the backend agent
// registry (src/deskmind/agents/registry.py), the agent's Redis health hash and agent_reports.

export default function AgentsPage() {
  const { data, error, loading, reload } = useApi(api.agents, 15000);

  return (
    <div className="flex-1 space-y-6 p-6">
      <PageHeader title="Agents" subtitle="Agents that exist in the backend registry, with their real version and runtime state.">
        <RefreshButton onClick={() => void reload()} />
      </PageHeader>
      <ErrorBox error={error} />
      <Loading loading={loading} />

      {data && data.agents.length === 0 && <Empty>No agents registered.</Empty>}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {data?.agents.map((agent) => {
          const rt = agent.runtime;
          const tone = rt.state === "running" ? "up" : rt.state === "stopped" ? "down" : "muted";
          return (
            <Card
              key={agent.name}
              title={
                <span>
                  {agent.name} <span className="font-mono text-xs font-normal text-muted">v{agent.version} · {agent.kind}</span>
                </span>
              }
              right={<Badge tone={tone}>{rt.state.replace("_", " ")}</Badge>}
            >
              <p className="text-xs text-muted">{agent.summary}</p>
              {rt.reason && <p className="text-[11px] text-muted">State reason: {rt.reason}</p>}

              <div className="rounded bg-surface-2 p-3">
                <KV k="Last heartbeat" v={fmtAge(rt.heartbeat_age_s)} />
                <KV k="Last report (bar close)" v={fmtTime(rt.last_report_as_of)} />
                <KV k="Last report stored" v={fmtTime(rt.last_report_stored_at)} />
                <KV k="Reports stored (24h)" v={val(rt.reports_stored_24h)} />
                <KV k="Reports since process start" v={val(rt.reports_since_start)} />
                <KV k="Errors (24h)" v={val(rt.errors_24h)} />
                <KV k="Last error" v={rt.last_error ? <span className="text-down">{rt.last_error}</span> : "none recorded"} />
                {rt.reports_error && <KV k="agent_reports query" v={<span className="text-down">{rt.reports_error}</span>} />}
              </div>

              <details className="text-xs">
                <summary className="cursor-pointer text-muted hover:text-foreground">Parameters ({agent.spec})</summary>
                {agent.spec_intro && <p className="mt-2 text-muted">{agent.spec_intro}</p>}
                <div className="mt-2 rounded bg-surface-2 p-3">
                  {Object.entries(agent.params).map(([k, v]) => (
                    <KV key={k} k={k} v={val(v)} />
                  ))}
                </div>
              </details>
            </Card>
          );
        })}

        {data?.planned.map((p) => (
          <Card key={p.name} title={<span className="text-muted">{p.name}</span>} right={<Badge tone="muted">planned</Badge>}>
            <Empty>Not built yet — Step {p.roadmap_step}</Empty>
            <p className="text-xs text-muted">{p.summary}</p>
          </Card>
        ))}
      </div>
    </div>
  );
}
