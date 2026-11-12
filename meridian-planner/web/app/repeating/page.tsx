"use client";

import { useEffect, useState } from "react";
import { RepeatEditor, describeRecurrence } from "@/components/RepeatEditor";
import { Shell } from "@/components/Shell";
import { TaskDrawer } from "@/components/TaskDrawer";
import { api, bus, useApi } from "@/lib/api";
import type { Recurrence, Series, Task } from "@/lib/types";

const MISSED: Record<string, string> = {
  keep: "Missed ones stay open",
  skip: "Missed ones are skipped",
  latest_only: "Only the newest stays open",
};

function fmtDay(d: string) {
  return new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function SeriesCard({ s, onChanged, onOpenTask }: { s: Series; onChanged: () => void; onOpenTask: (id: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [rule, setRule] = useState<Recurrence | null>(s.rule);
  const [title, setTitle] = useState(s.title);
  const [err, setErr] = useState<string | null>(null);
  const { data: occ, reload } = useApi<{ items: Task[] }>(`/tasks?view=all&series_id=${s.id}&sort=deadline&limit=50`);
  useEffect(() => bus.on("tasks", reload), [reload]);

  async function patch(body: object) {
    setErr(null);
    try {
      await api(`/series/${s.id}`, { method: "PATCH", json: body });
      onChanged();
      bus.emit("tasks");
      return true;
    } catch (e) {
      setErr((e as Error).message);
      return false;
    }
  }
  const open = (occ?.items ?? []).filter((t) => t.status !== "done" && t.status !== "cancelled");
  const done = (occ?.items ?? []).filter((t) => t.status === "done").length;

  return (
    <section id={s.id} className={`rounded-xl border bg-white p-4 dark:bg-stone-900 ${s.active ? "border-stone-200 dark:border-stone-800" : "border-dashed border-stone-300 opacity-75 dark:border-stone-700"}`}>
      <div className="flex flex-wrap items-start gap-2">
        <div>
          <h2 className="text-sm font-semibold">↻ {s.title}</h2>
          <div className="text-xs text-stone-500">
            {describeRecurrence(s.rule)} · due {s.rule.due_time ?? "17:00"} · {MISSED[s.missed_policy]}
            {!s.active && " · stopped"}
          </div>
        </div>
        <div className="ml-auto flex gap-2">
          <button className="btn" onClick={() => setEditing(!editing)}>
            {editing ? "Close" : "Edit"}
          </button>
          <button className="btn" onClick={() => patch({ active: !s.active })}>
            {s.active ? "Stop" : "Resume"}
          </button>
          <button
            className="text-xs text-stone-400 hover:text-red-600"
            onClick={async () => {
              const alsoOpen = confirm(`Delete the repeating task “${s.title}”?\n\nOK: also delete its ${open.length} open occurrence(s).\nCancel: keep them as one-off tasks.`);
              await api(`/series/${s.id}?delete_open=${alsoOpen}`, { method: "DELETE" });
              onChanged();
              bus.emit("tasks");
            }}
          >
            Delete
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap gap-2 text-xs">
        {open.map((t) => (
          <button key={t.id} onClick={() => onOpenTask(t.id)} className={`chip ${t.overdue ? "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-200" : "bg-stone-100 dark:bg-stone-800"}`}>
            {t.occurrence_due ? fmtDay(t.occurrence_due) : "—"}
            {t.scheduled_start && <span className="ml-1 text-stone-400">· planned {new Date(t.scheduled_start).toLocaleDateString(undefined, { weekday: "short" })}</span>}
          </button>
        ))}
        {s.upcoming.filter((d) => !open.some((t) => t.occurrence_due === d)).slice(0, 3).map((d) => (
          <span key={d} className="chip border border-dashed border-stone-300 text-stone-400 dark:border-stone-700">
            {fmtDay(d)}
          </span>
        ))}
        <span className="self-center text-stone-400">{done} done so far</span>
      </div>

      {editing && (
        <div className="mt-3 space-y-3 border-t border-stone-100 pt-3 dark:border-stone-800">
          <label className="block text-sm">
            <span className="label">Title (applies to occurrences not started yet)</span>
            <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
          <RepeatEditor value={rule} onChange={setRule} />
          <div className="flex gap-2">
            <button
              className="btn-primary"
              onClick={async () => {
                const body: Record<string, unknown> = {};
                if (title !== s.title) body.title = title;
                if (rule && JSON.stringify(rule) !== JSON.stringify(s.rule)) {
                  const { missed, ...r } = rule;
                  body.rule = r;
                  if (missed && missed !== s.missed_policy) body.missed_policy = missed;
                }
                if (!Object.keys(body).length || (await patch(body))) setEditing(false);
              }}
            >
              Save
            </button>
            <span className="self-center text-xs text-stone-500">Changing the schedule replaces upcoming occurrences you haven't started.</span>
          </div>
        </div>
      )}
      {err && <div className="mt-2 text-xs text-red-600">{err}</div>}
    </section>
  );
}

export default function RepeatingPage() {
  const { data, reload } = useApi<Series[]>("/series");
  const [taskId, setTaskId] = useState<string | null>(null);
  useEffect(() => bus.on("tasks", reload), [reload]);
  return (
    <Shell>
      <div className="flex h-full">
        <div className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-3xl space-y-3 p-4">
            <h1 className="text-lg font-semibold">Repeating tasks</h1>
            <p className="text-sm text-stone-500">
              Each occurrence is an ordinary task that the planner schedules before its due date. Create one from quick add (“Billing on the last business day of every month”) or in a task's Repeat section.
            </p>
            {data?.length === 0 && <div className="rounded-xl border border-dashed border-stone-300 p-6 text-center text-sm text-stone-500 dark:border-stone-700">No repeating tasks yet.</div>}
            {(data ?? []).map((s) => (
              <SeriesCard key={s.id} s={s} onChanged={reload} onOpenTask={setTaskId} />
            ))}
          </div>
        </div>
        {taskId && <TaskDrawer taskId={taskId} onClose={() => setTaskId(null)} />}
      </div>
    </Shell>
  );
}
