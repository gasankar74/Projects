"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { BarList, CapacityRows, Card, Stat } from "@/components/Charts";
import { Shell, useApp } from "@/components/Shell";
import { PlayButton, fmtClock, stopTimer, useTimer } from "@/components/Timer";
import { PRIORITY_STYLE } from "@/components/TaskRow";
import { api, bus, useApi } from "@/lib/api";
import { fmtDeadline, fmtDuration, fmtTime } from "@/lib/dates";
import { PRIORITY_LABEL, type Task, type Top3 } from "@/lib/types";

interface AgendaItem {
  kind: "task" | "event";
  start: string;
  end: string;
  title: string;
  task_id?: string;
  all_day?: boolean;
  busy?: boolean;
  color?: string | null;
}

interface Week {
  start: string;
  end: string;
  days: { date: string; work_min: number | null; free_min: number | null; planned_min: number; tracked_min: number; done: number }[];
  totals: { work_min: number; planned_min: number; tracked_min: number; done: number; created: number; overdue: number; tasks_worked_on: number };
  tracked_by_category: { key: string; label: string; color: string | null; minutes: number }[];
  deadlines: { task_id: string; title: string; deadline: string; deadline_type: string; finish: string | null; at_risk: boolean }[];
  risks: { kind: string; task_id: string | null; message: string }[];
  done_tasks: { task_id: string; title: string; completed_at: string }[];
  estimate_vs_actual: { task_id: string; title: string; estimate_min: number; actual_min: number }[];
}

function todayIso() {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function TopThree() {
  const { openTask, runningTaskId } = useApp();
  const day = todayIso();
  const { data, reload } = useApi<Top3>(`/focus/top3?day=${day}`);
  const [editing, setEditing] = useState(false);
  const [pick, setPick] = useState<string[]>([]);
  const { data: open } = useApi<{ items: Task[] }>(editing ? "/tasks?view=active&sort=smart&limit=60" : null);
  useEffect(() => bus.on("tasks", reload), [reload]);
  useEffect(() => bus.on("schedule", reload), [reload]);

  const save = async (ids: string[]) => {
    await api("/focus/top3", { method: "PUT", json: { day, task_ids: ids } });
    setEditing(false);
    reload();
  };

  return (
    <Card
      title="Today's Top 3"
      action={
        <div className="flex items-center gap-2 text-xs text-stone-500">
          {data && <span>{data.source === "you" ? "Your pick" : "Suggested from your plan"}</span>}
          {!editing ? (
            <button
              className="btn text-xs"
              onClick={() => {
                setPick(data?.items.map((i) => i.task_id) ?? []);
                setEditing(true);
              }}
            >
              Choose
            </button>
          ) : (
            <button className="btn text-xs" onClick={() => setEditing(false)}>
              Close
            </button>
          )}
        </div>
      }
    >
      {!editing && (
        <ol className="space-y-2">
          {(data?.items ?? []).map((it, i) => (
            <li key={it.task_id} className="group flex items-center gap-3 rounded-lg border border-stone-100 p-2.5 dark:border-stone-800">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-50 text-sm font-semibold text-brand-700 dark:bg-brand-700/25 dark:text-brand-100">{i + 1}</span>
              <button className="min-w-0 flex-1 text-left" onClick={() => openTask(it.task_id)}>
                <div className={`truncate text-sm font-medium ${it.done ? "text-stone-400 line-through" : ""}`}>{it.title}</div>
                <div className="flex flex-wrap gap-x-2 text-[11px] text-stone-500">
                  <span className={`chip ${PRIORITY_STYLE[it.priority]}`}>{PRIORITY_LABEL[it.priority]}</span>
                  {it.start && !it.done && <span>▸ {fmtDeadline(it.start)}</span>}
                  {it.deadline && <span>Due {fmtDeadline(it.deadline)}</span>}
                  {it.at_risk && !it.done && <span className="font-medium text-orange-700 dark:text-orange-300">At risk</span>}
                  <span>{it.why}</span>
                </div>
              </button>
              {!it.done && <PlayButton taskId={it.task_id} running={runningTaskId === it.task_id} />}
              {!it.done && (
                <button
                  className="rounded-md px-2 py-1 text-xs text-stone-500 hover:bg-emerald-50 hover:text-emerald-700 dark:hover:bg-emerald-950/40"
                  onClick={async () => {
                    await api(`/tasks/${it.task_id}/complete`, { method: "POST" });
                    bus.emit("tasks");
                  }}
                >
                  Done
                </button>
              )}
            </li>
          ))}
          {data && data.items.length === 0 && <li className="text-sm text-stone-400">Nothing planned today. Choose up to three tasks to focus on.</li>}
        </ol>
      )}
      {editing && (
        <div>
          <p className="mb-2 text-xs text-stone-500">Pick up to three, in order. ({pick.length}/3)</p>
          <div className="max-h-72 space-y-0.5 overflow-y-auto">
            {(open?.items ?? []).map((t) => {
              const idx = pick.indexOf(t.id);
              return (
                <label key={t.id} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-sm hover:bg-stone-50 dark:hover:bg-stone-800">
                  <input
                    type="checkbox"
                    checked={idx >= 0}
                    disabled={idx < 0 && pick.length >= 3}
                    onChange={() => setPick((p) => (idx >= 0 ? p.filter((x) => x !== t.id) : [...p, t.id]))}
                  />
                  {idx >= 0 && <span className="text-xs font-semibold text-brand-700">{idx + 1}</span>}
                  <span className="truncate">{t.title}</span>
                  {t.deadline && <span className="ml-auto shrink-0 text-xs text-stone-500">{fmtDeadline(t.deadline)}</span>}
                </label>
              );
            })}
          </div>
          <div className="mt-3 flex gap-2">
            <button className="btn-primary" onClick={() => save(pick)}>
              Save
            </button>
            <button className="btn" onClick={() => save([])} title="Go back to the suggestion from your plan">
              Use suggestion
            </button>
          </div>
        </div>
      )}
    </Card>
  );
}

function RunningTimer() {
  const { entry, elapsed } = useTimer();
  if (!entry) return null;
  const left = Math.max(0, (entry.remaining_min ?? 0) * 60 - elapsed);
  return (
    <Card title="Tracking now" action={<Link href="/focus" className="text-xs text-brand-700 hover:underline dark:text-brand-100">Focus mode →</Link>}>
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{entry.title}</div>
          <div className="text-xs text-stone-500">About {fmtDuration(Math.round(left / 60))} of the estimate left</div>
        </div>
        <span className="font-mono text-2xl tabular-nums">{fmtClock(elapsed)}</span>
        <button className="btn" onClick={() => stopTimer()}>
          Stop
        </button>
        <button className="btn-primary" onClick={() => stopTimer({ complete: true })}>
          Done
        </button>
      </div>
    </Card>
  );
}

function Agenda() {
  const { openTask } = useApp();
  const { data, reload } = useApi<{ items: AgendaItem[]; planned_min: number }>(`/schedule/agenda?day=${todayIso()}`);
  useEffect(() => bus.on("schedule", reload), [reload]);
  useEffect(() => bus.on("tasks", reload), [reload]);
  const now = Date.now();
  return (
    <Card title="Today's plan" action={data && <span className="text-xs text-stone-500">{fmtDuration(data.planned_min)} of planned work</span>}>
      <ul className="space-y-1">
        {(data?.items ?? []).map((it, i) => {
          const past = new Date(it.end).getTime() < now;
          return (
            <li key={i} className={`flex items-center gap-3 text-sm ${past ? "opacity-50" : ""}`}>
              <span className="w-28 shrink-0 text-xs tabular-nums text-stone-500">{it.all_day ? "All day" : `${fmtTime(new Date(it.start))} – ${fmtTime(new Date(it.end))}`}</span>
              {it.kind === "task" ? (
                <button className="min-w-0 flex-1 truncate rounded-md border-l-4 border-brand-500 bg-brand-50 px-2 py-1 text-left dark:bg-brand-700/20" onClick={() => it.task_id && openTask(it.task_id)}>
                  {it.title}
                </button>
              ) : (
                <span className={`min-w-0 flex-1 truncate rounded-md border-l-4 border-stone-300 bg-stone-50 px-2 py-1 dark:border-stone-600 dark:bg-stone-800/60 ${it.busy === false ? "italic text-stone-500" : ""}`}>{it.title}</span>
              )}
            </li>
          );
        })}
        {data && data.items.length === 0 && <li className="text-sm text-stone-400">Nothing on the calendar or planned today.</li>}
      </ul>
    </Card>
  );
}

function WeekView() {
  const { openTask } = useApp();
  const [offset, setOffset] = useState(0);
  const monday = (() => {
    const d = new Date();
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7) + offset * 7);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  })();
  const { data: w, reload } = useApi<Week>(`/dashboard/week?start=${monday}`);
  useEffect(() => bus.on("schedule", reload), [reload]);
  useEffect(() => bus.on("timer", reload), [reload]);
  if (!w) return null;
  const range = `${new Date(w.start + "T12:00").toLocaleDateString([], { month: "short", day: "numeric" })} – ${new Date(w.end + "T12:00").toLocaleDateString([], { month: "short", day: "numeric" })}`;
  const load = w.totals.work_min ? Math.round((100 * w.totals.planned_min) / w.totals.work_min) : 0;
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Week of {range}</h2>
        <div className="ml-auto flex gap-1">
          <button className="btn text-xs" onClick={() => setOffset(offset - 1)} aria-label="Previous week">
            ←
          </button>
          {offset !== 0 && (
            <button className="btn text-xs" onClick={() => setOffset(0)}>
              This week
            </button>
          )}
          <button className="btn text-xs" onClick={() => setOffset(offset + 1)} aria-label="Next week">
            →
          </button>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Planned work" value={fmtDuration(w.totals.planned_min)} sub={`${load}% of ${fmtDuration(w.totals.work_min)} working time`} />
        <Stat label="Time tracked" value={fmtDuration(w.totals.tracked_min)} sub={`on ${w.totals.tasks_worked_on} tasks`} />
        <Stat label="Completed" value={String(w.totals.done)} sub={`${w.totals.created} added this week`} />
        <Stat label="Overdue now" value={String(w.totals.overdue)} sub={w.risks.length ? `${w.risks.length} plan warnings` : "No plan warnings"} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Load by day">
          <CapacityRows days={w.days.map((d) => ({ ...d, extra: d.tracked_min ? `${fmtDuration(d.tracked_min)} tracked` : undefined }))} />
        </Card>
        <Card title="Time tracked by category">
          <BarList rows={w.tracked_by_category} empty="No time tracked this week. Start a timer from any task with ▶." />
        </Card>
        <Card title="Deadlines this week">
          <ul className="space-y-1 text-sm">
            {w.deadlines.map((d) => (
              <li key={d.task_id} className="flex items-center gap-2">
                <button className="min-w-0 flex-1 truncate text-left hover:underline" onClick={() => openTask(d.task_id)}>
                  {d.title}
                </button>
                {d.at_risk && <span className="chip bg-orange-100 text-orange-800 dark:bg-orange-500/20 dark:text-orange-200">At risk</span>}
                <span className="shrink-0 text-xs text-stone-500">{fmtDeadline(d.deadline)}</span>
              </li>
            ))}
            {w.deadlines.length === 0 && <li className="text-stone-400">No deadlines this week.</li>}
          </ul>
        </Card>
        <Card title="Needs attention">
          <ul className="space-y-1.5 text-sm">
            {w.risks.map((r, i) => (
              <li key={i}>
                <button className="text-left hover:underline" onClick={() => r.task_id && openTask(r.task_id)}>
                  ⚠ {r.message}
                </button>
              </li>
            ))}
            {w.risks.length === 0 && <li className="text-stone-400">Everything fits before its deadline.</li>}
          </ul>
        </Card>
      </div>
    </div>
  );
}

function Today() {
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4">
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="space-y-4">
            <RunningTimer />
            <TopThree />
          </div>
          <Agenda />
        </div>
        <WeekView />
      </div>
    </div>
  );
}

export default function TodayPage() {
  return (
    <Shell>
      <Today />
    </Shell>
  );
}
