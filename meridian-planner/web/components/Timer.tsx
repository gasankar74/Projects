"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, bus } from "@/lib/api";
import type { TimeEntry } from "@/lib/types";

export function fmtClock(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

export async function startTimer(taskId: string, source: "timer" | "focus" = "timer") {
  await api("/time/start", { method: "POST", json: { task_id: taskId, source } });
  bus.emit("timer");
  bus.emit("tasks");
}

export async function stopTimer(opts: { complete?: boolean; reduce_remaining?: boolean } = {}) {
  await api("/time/stop", { method: "POST", json: { reduce_remaining: opts.reduce_remaining ?? true, complete: !!opts.complete } });
  bus.emit("timer");
  bus.emit("tasks");
}

/** The running timer entry (if any), refreshed on timer events. Doesn't tick. */
export function useRunningEntry() {
  const [entry, setEntry] = useState<TimeEntry | null>(null);
  const reload = useCallback(async () => {
    try {
      setEntry(await api<TimeEntry | null>("/time/current"));
    } catch {}
  }, []);
  useEffect(() => {
    reload();
    const off = bus.on("timer", reload);
    const onFocus = () => document.visibilityState === "visible" && reload();
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      off();
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [reload]);
  return { entry, reload };
}

/** The running timer with a ticking elapsed time in seconds. */
export function useTimer() {
  const { entry, reload } = useRunningEntry();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!entry) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [entry]);
  const elapsed = entry ? (now - new Date(entry.started_at).getTime()) / 1000 : 0;
  return { entry, elapsed, reload };
}

/** Compact running-timer pill for the header. */
export function TimerBar() {
  const { entry, elapsed } = useTimer();
  const [busy, setBusy] = useState(false);
  if (!entry) return null;
  return (
    <div className="flex min-w-0 items-center gap-1.5 rounded-full bg-brand-50 py-0.5 pr-1 pl-2.5 text-xs text-brand-700 dark:bg-brand-700/25 dark:text-brand-100">
      <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-brand-500" aria-hidden />
      <Link href="/focus" className="max-w-[10rem] truncate font-medium hover:underline" title={`Tracking “${entry.title}”. Open focus mode`}>
        {entry.title}
      </Link>
      <span className="font-mono tabular-nums">{fmtClock(elapsed)}</span>
      <button
        className="rounded-full px-2 py-0.5 font-medium hover:bg-brand-100 dark:hover:bg-brand-700/40"
        disabled={busy}
        title="Stop and log the time against the task's remaining estimate"
        onClick={async () => {
          setBusy(true);
          try {
            await stopTimer();
          } finally {
            setBusy(false);
          }
        }}
      >
        Stop
      </button>
    </div>
  );
}

/** Small play button for task rows. */
export function PlayButton({ taskId, running }: { taskId: string; running: boolean }) {
  return (
    <button
      onClick={async (e) => {
        e.stopPropagation();
        if (running) await stopTimer();
        else await startTimer(taskId);
      }}
      title={running ? "Stop the timer" : "Start a timer on this task"}
      aria-label={running ? "Stop timer" : "Start timer"}
      className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] ${running ? "bg-brand-600 text-white" : "text-stone-400 opacity-0 group-hover:opacity-100 hover:bg-stone-200 focus:opacity-100 dark:hover:bg-stone-700"}`}
    >
      {running ? "■" : "▶"}
    </button>
  );
}
