"use client";

import { useEffect, useState } from "react";
import { api, bus, useApi } from "@/lib/api";
import { fmtDuration } from "@/lib/dates";
import type { PlanWarning, Schedule } from "@/lib/types";
import { useApp } from "./Shell";

function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

const TONE: Record<string, string> = {
  cannot_meet_deadline: "border-red-200 bg-red-50 text-red-900 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-100",
  at_risk: "border-orange-200 bg-orange-50 text-orange-900 dark:border-orange-500/30 dark:bg-orange-500/10 dark:text-orange-100",
  will_miss_soft_deadline: "border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-100",
};
const ORDER = ["cannot_meet_deadline", "locked_conflict", "at_risk", "will_miss_soft_deadline", "blocked_by_prerequisite", "no_slot_long_enough", "unscheduled"];

/** Plan freshness, replan/undo, and the scheduler's warnings (deadline risk, overload and what to defer). */
export function PlanStatus() {
  const { openTask } = useApp();
  const { data, reload, setData } = useApi<Schedule>("/schedule?start=" + encodeURIComponent(new Date().toISOString()) + "&end=" + encodeURIComponent(new Date(Date.now() + 86400000).toISOString()));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => bus.on("tasks", reload), [reload]);
  useEffect(() => bus.on("calendars", reload), [reload]);
  useEffect(() => bus.on("schedule", reload), [reload]);

  async function act(path: string, json?: unknown) {
    setBusy(true);
    setErr(null);
    try {
      setData(await api<Schedule>(path, { method: "POST", json }));
      bus.emit("schedule");
      bus.emit("tasks");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const warnings = [...(data?.warnings ?? [])].sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
  const serious = warnings.filter((w) => w.kind === "cannot_meet_deadline" || w.kind === "at_risk" || w.kind === "locked_conflict");
  const shown = open ? warnings : serious.slice(0, 3);
  // The next day that has working time (late in the evening, "today" has none left).
  const next = data?.days?.find((d) => d.free_min > 0 || d.planned_min > 0);
  const nextLabel = (() => {
    if (!next) return "";
    const [y, m, d] = next.date.split("-").map(Number);
    const day = new Date(y, m - 1, d);
    const t0 = new Date();
    t0.setHours(0, 0, 0, 0);
    const diff = Math.round((day.getTime() - t0.getTime()) / 86400000);
    return diff === 0 ? "today" : diff === 1 ? "tomorrow" : day.toLocaleDateString([], { weekday: "long" });
  })();

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-stone-500">
        <span className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${data?.dirty ? "bg-amber-500" : "bg-emerald-500"}`} />
          {data?.run ? `Planned ${ago(data.run.created_at)}` : "Not planned yet"}
        </span>
        {next && (
          <span>
            · {nextLabel}: {fmtDuration(next.planned_min)} planned of {fmtDuration(next.free_min)} free
          </span>
        )}
        <div className="ml-auto flex gap-1.5">
          <button className="btn !px-2 !py-1 text-xs" disabled={busy} onClick={() => act("/schedule/replan", { fresh: false })} title="Replan now">
            Replan
          </button>
          <button className="btn !px-2 !py-1 text-xs" disabled={busy || !data?.can_undo} onClick={() => act("/schedule/undo")} title="Go back to the previous plan">
            Undo
          </button>
        </div>
      </div>
      {data?.run && <div className="truncate text-[11px] text-stone-400" title={data.run.trigger}>Last change: {data.run.trigger}</div>}
      {err && <div className="text-xs text-red-600">{err}</div>}

      {data?.overload && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-2.5 text-xs text-red-900 dark:border-red-500/30 dark:bg-red-500/10 dark:text-red-100">
          <div className="font-semibold">Overloaded</div>
          <div className="mt-0.5">{data.overload.message}</div>
          {data.overload.defer.length > 0 ? (
            <div className="mt-1.5">
              <div className="font-medium">Defer these to fit everything else:</div>
              <ul className="mt-0.5 list-disc pl-4">
                {data.overload.defer.map((d) => (
                  <li key={d.task_id}>
                    <button className="underline" onClick={() => openTask(d.task_id)}>
                      {d.title}
                    </button>{" "}
                    ({fmtDuration(d.minutes)})
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <div className="mt-1">Even deferring lower-priority work won&apos;t fit it all. Move a deadline or shorten a task.</div>
          )}
        </div>
      )}

      {shown.map((w: PlanWarning, i) => (
        <div key={i} className={`rounded-lg border p-2.5 text-xs ${TONE[w.kind] ?? "border-stone-200 bg-stone-50 text-stone-700 dark:border-stone-700 dark:bg-stone-800 dark:text-stone-200"}`}>
          {w.task_id ? (
            <button className="text-left" onClick={() => openTask(w.task_id!)}>
              {w.message}
            </button>
          ) : (
            w.message
          )}
        </div>
      ))}
      {warnings.length > shown.length && (
        <button className="text-xs text-brand-600 hover:underline" onClick={() => setOpen(true)}>
          Show {warnings.length - shown.length} more notes
        </button>
      )}
    </div>
  );
}
