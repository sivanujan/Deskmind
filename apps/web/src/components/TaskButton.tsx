"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type TaskKind, type TaskState } from "@/lib/api";
import { fmtTime } from "@/components/ui";

/**
 * Starts a one-off backend job (login required) and shows its real state from
 * GET /api/control/tasks until it finishes. `onDone` lets the page reload its data.
 */
export default function TaskButton({
  kind,
  label,
  start,
  confirmText,
  onDone,
}: {
  kind: TaskKind;
  label: string;
  start: () => Promise<TaskState>;
  confirmText?: string;
  onDone?: () => void;
}) {
  const [task, setTask] = useState<TaskState | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const onDoneRef = useRef(onDone);
  useEffect(() => {
    onDoneRef.current = onDone;
  });

  const poll = useCallback(async () => {
    try {
      const all = await api.tasks();
      const t = all[kind];
      setTask((prev) => {
        if (prev?.state === "running" && t && t.state !== "running") onDoneRef.current?.();
        return t;
      });
    } catch {
      // the page's own data request shows API errors
    }
  }, [kind]);

  useEffect(() => {
    const t = setTimeout(() => void poll(), 0); // initial state of the last run
    return () => clearTimeout(t);
  }, [poll]);

  useEffect(() => {
    if (task?.state !== "running") return;
    const t = setInterval(() => void poll(), 2000);
    return () => clearInterval(t);
  }, [task?.state, poll]);

  const click = async () => {
    if (confirmText && !window.confirm(confirmText)) return;
    setErr(null);
    try {
      setTask(await start());
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(/authentication required|401/i.test(msg) ? "Log in first (Control Login, top right)." : msg);
    }
  };

  const running = task?.state === "running";
  return (
    <div className="flex flex-col items-end gap-1 text-xs">
      <button
        onClick={() => void click()}
        disabled={running}
        className="rounded border border-accent/40 bg-accent/20 px-3 py-1.5 font-semibold text-accent hover:bg-accent/30 disabled:opacity-50"
      >
        {running ? `${label}: running…` : label}
      </button>
      {err && <span className="text-down">{err}</span>}
      {task && !running && (
        <span className={task.state === "done" ? "text-up" : "text-down"}>
          Last run {task.state} · {fmtTime(task.finished_at)}
          {task.error ? `: ${task.error}` : ""}
        </span>
      )}
      {running && <span className="text-muted">started {fmtTime(task.started_at)} by {task.user}</span>}
    </div>
  );
}
