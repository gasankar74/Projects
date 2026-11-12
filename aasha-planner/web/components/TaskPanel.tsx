"use client";

import Link from "next/link";
import { useEffect } from "react";
import { bus, useApi } from "@/lib/api";
import { addDays, fmtDuration, startOfDay } from "@/lib/dates";
import type { Task } from "@/lib/types";
import { TaskRow } from "./TaskRow";

function Group({ title, tasks, tone }: { title: string; tasks: Task[]; tone?: string }) {
  if (!tasks.length) return null;
  const min = tasks.reduce((a, t) => a + t.remaining_min, 0);
  return (
    <div>
      <div className={`sticky top-0 z-10 flex items-center justify-between bg-stone-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide dark:bg-stone-900 ${tone ?? "text-stone-500"}`}>
        <span>
          {title} · {tasks.length}
        </span>
        <span className="font-normal normal-case">{fmtDuration(min)}</span>
      </div>
      {tasks.map((t) => (
        <TaskRow key={t.id} task={t} />
      ))}
    </div>
  );
}

/** Right-hand panel: open tasks in priority order, grouped by urgency. */
export function TaskPanel() {
  const { data, reload } = useApi<{ items: Task[]; total: number }>("/tasks?limit=300");
  useEffect(() => bus.on("tasks", reload), [reload]);
  const tasks = data?.items ?? [];
  const today = startOfDay(new Date());
  const tomorrow = addDays(today, 1);
  const week = addDays(today, 7);
  const due = (t: Task) => (t.deadline ? new Date(t.deadline) : null);
  const overdue = tasks.filter((t) => t.overdue);
  const todayList = tasks.filter((t) => !t.overdue && due(t) && due(t)! < tomorrow);
  const weekList = tasks.filter((t) => due(t) && due(t)! >= tomorrow && due(t)! < week);
  const later = tasks.filter((t) => due(t) && due(t)! >= week);
  const none = tasks.filter((t) => !due(t));
  const totalMin = tasks.reduce((a, t) => a + t.remaining_min, 0);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-stone-200 px-3 py-2 dark:border-stone-800">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold">Priorities</h2>
          <Link href="/tasks" className="text-xs text-brand-600 hover:underline">
            All tasks
          </Link>
        </div>
        <div className="mt-1 text-xs text-stone-500">
          {data ? `${data.total} open · ${fmtDuration(totalMin)} of work remaining` : "Loading…"}
        </div>
        <div className="mt-2 rounded-md bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800 dark:bg-amber-500/10 dark:text-amber-200">
          Automatic scheduling arrives in Phase 2. For now this list is ordered by overdue, priority, then deadline.
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        <Group title="Overdue" tasks={overdue} tone="text-red-600" />
        <Group title="Due today" tasks={todayList} />
        <Group title="Next 7 days" tasks={weekList} />
        <Group title="Later" tasks={later} />
        <Group title="No deadline" tasks={none} />
        {data && tasks.length === 0 && <div className="p-6 text-center text-sm text-stone-500">Nothing open. Press Q to add a task.</div>}
      </div>
    </div>
  );
}
