"use client";

import { describeRecurrence } from "./RepeatEditor";
import { useState } from "react";
import { api, bus } from "@/lib/api";
import { fmtDeadline, fmtDuration } from "@/lib/dates";
import type { Task } from "@/lib/types";
import { PRIORITY_LABEL } from "@/lib/types";
import { useApp } from "./Shell";

export const PRIORITY_STYLE: Record<string, string> = {
  urgent: "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-300",
  high: "bg-orange-100 text-orange-700 dark:bg-orange-500/20 dark:text-orange-300",
  medium: "bg-sky-100 text-sky-700 dark:bg-sky-500/20 dark:text-sky-300",
  low: "bg-stone-100 text-stone-600 dark:bg-stone-700/50 dark:text-stone-300",
};

export function TaskRow({ task, compact = false }: { task: Task; compact?: boolean }) {
  const { categories, projects, openTask } = useApp();
  const [done, setDone] = useState(task.status === "done");
  const cat = categories.find((c) => c.id === task.category_id);
  const proj = projects.find((p) => p.id === task.project_id);

  async function toggle(e: React.MouseEvent) {
    e.stopPropagation();
    const next = !done;
    setDone(next);
    try {
      await api(`/tasks/${task.id}/${next ? "complete" : "reopen"}`, { method: "POST" });
      bus.emit("tasks");
    } catch {
      setDone(!next);
    }
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => openTask(task.id)}
      onKeyDown={(e) => e.key === "Enter" && openTask(task.id)}
      className={`group flex cursor-pointer items-start gap-2.5 border-b border-stone-100 px-3 py-2 hover:bg-stone-50 dark:border-stone-800 dark:hover:bg-stone-800/50 ${task.overdue && !done ? "bg-red-50/60 dark:bg-red-950/20" : ""}`}
    >
      <button
        onClick={toggle}
        title={done ? "Mark not done" : "Complete"}
        className={`mt-0.5 flex h-4.5 w-4.5 shrink-0 items-center justify-center rounded-full border-2 text-[10px] ${done ? "border-emerald-500 bg-emerald-500 text-white" : "border-stone-300 hover:border-emerald-500 dark:border-stone-600"}`}
        style={{ width: 18, height: 18 }}
      >
        {done ? "✓" : ""}
      </button>
      <div className="min-w-0 flex-1">
        <div className={`flex items-center gap-1.5 text-sm ${done ? "text-stone-400 line-through" : ""}`}>
          {task.confidential && <span title="Confidential">🔒</span>}
          <span className="truncate">{task.title}</span>
          {task.recurrence && <span title={`Repeats: ${describeRecurrence(task.recurrence)}`} className="text-stone-400">↻</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-stone-500">
          <span className={`chip ${PRIORITY_STYLE[task.priority]}`}>{PRIORITY_LABEL[task.priority]}</span>
          {task.deadline && (
            <span className={task.overdue ? "font-semibold text-red-600" : ""} title={task.deadline_type === "hard" ? "Hard deadline" : "Target date"}>
              {task.overdue ? "Overdue · " : task.deadline_type === "hard" ? "Due " : "Target "}
              {fmtDeadline(task.deadline)}
            </span>
          )}
          <span>
            {task.remaining_min < task.duration_min ? `${fmtDuration(task.remaining_min)} left of ${fmtDuration(task.duration_min)}` : fmtDuration(task.duration_min)}
          </span>
          {!compact && cat && (
            <span className="flex items-center gap-1">
              <span className="h-2 w-2 rounded-full" style={{ background: cat.color }} />
              {cat.name}
            </span>
          )}
          {!compact && proj && <span>· {proj.name}</span>}
          {task.depends_on.length > 0 && <span title="Waiting on another task">⧗ blocked by {task.depends_on.length}</span>}
          {!done && task.scheduled_start && (
            <span className="text-brand-700 dark:text-brand-100" title="Next scheduled work on this task">
              ▸ {fmtDeadline(task.scheduled_start)}
            </span>
          )}
          {!done && task.auto_schedule && task.schedule_status === "unscheduled" && <span className="chip bg-stone-200 text-stone-700 dark:bg-stone-700 dark:text-stone-200">Not scheduled</span>}
          {!done && task.at_risk && <span className="chip bg-orange-100 text-orange-800 dark:bg-orange-500/20 dark:text-orange-200">At risk</span>}
          {task.status === "inbox" && <span className="chip bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-200">Inbox</span>}
        </div>
      </div>
    </div>
  );
}
