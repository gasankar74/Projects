"use client";

import { useEffect, useRef, useState } from "react";
import { Shell } from "@/components/Shell";
import { fmtClock, startTimer, stopTimer, useTimer } from "@/components/Timer";
import { api, bus, useApi } from "@/lib/api";
import { fmtDuration } from "@/lib/dates";
import type { Task, Top3 } from "@/lib/types";

const SESSIONS = [25, 50, 90];

function readLen(): number {
  try {
    return Number(localStorage.getItem("meridian-focus-len")) || 25;
  } catch {
    return 25;
  }
}

function Focus() {
  const { entry, elapsed } = useTimer();
  const { data: task, reload: reloadTask } = useApi<Task>(entry ? `/tasks/${entry.task_id}` : null);
  const { data: top } = useApi<Top3>(entry ? null : "/focus/top3");
  const [len, setLen] = useState(25);
  const [ended, setEnded] = useState(false);
  const notified = useRef<string | null>(null);
  useEffect(() => setLen(readLen()), []);

  const sessionLeft = len * 60 - elapsed;
  useEffect(() => {
    if (!entry || sessionLeft > 0 || notified.current === entry.id) return;
    notified.current = entry.id;
    setEnded(true);
    try {
      if (typeof Notification !== "undefined" && Notification.permission === "granted") new Notification("Focus session done", { body: `${len} minutes on “${entry.title}”. Time for a break?` });
    } catch {}
  }, [entry, sessionLeft, len]);
  useEffect(() => {
    if (entry) document.title = `${fmtClock(Math.max(0, sessionLeft))} · ${entry.title}`;
    return () => {
      document.title = "Elevate";
    };
  }, [entry, sessionLeft]);

  if (!entry) {
    return (
      <div className="mx-auto max-w-lg p-6">
        <h1 className="mb-1 text-lg font-semibold">Focus</h1>
        <p className="mb-4 text-sm text-stone-500">Pick a task. The time you spend comes off its estimate, so the plan stays honest.</p>
        <div className="mb-4 flex gap-2">
          {SESSIONS.map((m) => (
            <button
              key={m}
              className={len === m ? "btn-primary" : "btn"}
              onClick={() => {
                setLen(m);
                try {
                  localStorage.setItem("meridian-focus-len", String(m));
                } catch {}
              }}
            >
              {m} min
            </button>
          ))}
        </div>
        <ul className="space-y-2">
          {(top?.items ?? []).filter((i) => !i.done).map((i) => (
            <li key={i.task_id}>
              <button
                className="w-full rounded-lg border border-stone-200 bg-white p-3 text-left text-sm hover:border-brand-500 dark:border-stone-800 dark:bg-stone-900"
                onClick={async () => {
                  try {
                    if (typeof Notification !== "undefined" && Notification.permission === "default") await Notification.requestPermission();
                  } catch {}
                  notified.current = null;
                  setEnded(false);
                  await startTimer(i.task_id, "focus");
                }}
              >
                <span className="font-medium">▶ {i.title}</span>
                <span className="block text-xs text-stone-500">{i.why}</span>
              </button>
            </li>
          ))}
          {top && top.items.filter((i) => !i.done).length === 0 && <li className="text-sm text-stone-400">No Top 3 for today. Start a timer from any task with ▶.</li>}
        </ul>
      </div>
    );
  }

  const pct = Math.min(100, (100 * elapsed) / (len * 60));
  return (
    <div className="mx-auto flex max-w-lg flex-col items-center p-6 text-center">
      <div className="text-xs tracking-wide text-stone-500 uppercase">{ended ? "Session complete" : `${len}-minute focus session`}</div>
      <h1 className="mt-2 text-xl font-semibold">{entry.title}</h1>
      <div className={`my-6 font-mono text-6xl tabular-nums ${ended ? "text-emerald-600" : ""}`}>{fmtClock(Math.abs(sessionLeft))}</div>
      <div className="h-1.5 w-full rounded-full bg-stone-200 dark:bg-stone-800" aria-hidden>
        <div className="h-1.5 rounded-full bg-brand-500 transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      <div className="mt-2 text-xs text-stone-500">
        {fmtClock(elapsed)} tracked this session{task ? ` · about ${fmtDuration(Math.max(0, task.remaining_min - Math.floor(elapsed / 60)))} of the estimate left` : ""}
      </div>
      {task && task.checklist.length > 0 && (
        <ul className="mt-6 w-full space-y-1 text-left text-sm">
          {task.checklist.map((c) => (
            <li key={c.id}>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={c.done}
                  onChange={async (e) => {
                    await api(`/tasks/${task.id}/checklist/${c.id}`, { method: "PATCH", json: { done: e.target.checked } });
                    reloadTask();
                  }}
                />
                <span className={c.done ? "text-stone-400 line-through" : ""}>{c.text}</span>
              </label>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-8 flex gap-2">
        <button className="btn" onClick={() => stopTimer()}>
          Stop
        </button>
        <button
          className="btn-primary"
          onClick={async () => {
            await stopTimer({ complete: true });
            bus.emit("tasks");
          }}
        >
          Done with this task
        </button>
      </div>
    </div>
  );
}

export default function FocusPage() {
  return (
    <Shell>
      <div className="h-full overflow-y-auto">
        <Focus />
      </div>
    </Shell>
  );
}
