"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo } from "react";
import { Shell, useApp } from "@/components/Shell";
import { TaskRow } from "@/components/TaskRow";
import { bus, useApi } from "@/lib/api";
import { fmtDuration } from "@/lib/dates";
import type { Task } from "@/lib/types";
import { PRIORITIES, PRIORITY_LABEL } from "@/lib/types";

const VIEWS: Record<string, string> = { active: "Open tasks", inbox: "Inbox", overdue: "Overdue", history: "Completed", all: "Everything" };

function TaskList() {
  const sp = useSearchParams();
  const router = useRouter();
  const { categories, projects, openQuickAdd } = useApp();
  const view = sp.get("view") ?? "active";
  const category = sp.get("category") ?? "";
  const project = sp.get("project") ?? "";
  const priority = sp.get("priority") ?? "";
  const sort = sp.get("sort") ?? "smart";
  const q = sp.get("q") ?? "";
  const maxDur = sp.get("max_duration") ?? "";

  const query = useMemo(() => {
    const p = new URLSearchParams({ view, sort, limit: "500" });
    if (category) p.set("category_id", category);
    if (project) p.set("project_id", project);
    if (priority) p.set("priority", priority);
    if (q) p.set("q", q);
    if (maxDur) p.set("max_duration", maxDur);
    return p.toString();
  }, [view, category, project, priority, sort, q, maxDur]);
  const { data, reload, loading } = useApi<{ items: Task[]; total: number }>(`/tasks?${query}`);
  useEffect(() => bus.on("tasks", reload), [reload]);

  const setParam = (k: string, v: string) => {
    const p = new URLSearchParams(sp.toString());
    if (v) p.set(k, v);
    else p.delete(k);
    router.push(`/tasks?${p.toString()}`);
  };

  const heading = category ? categories.find((c) => c.id === category)?.name : project ? projects.find((p) => p.id === project)?.name : VIEWS[view];
  const items = data?.items ?? [];
  const min = items.reduce((a, t) => a + t.remaining_min, 0);

  return (
    <div className="flex h-full flex-col bg-white dark:bg-stone-900">
      <div className="space-y-2 border-b border-stone-200 px-4 py-3 dark:border-stone-800">
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold">{heading ?? "Tasks"}</h1>
          <span className="text-sm text-stone-500">
            {data ? `${data.total} task${data.total === 1 ? "" : "s"}${view !== "history" ? ` · ${fmtDuration(min)}` : ""}` : ""}
            {q ? ` matching “${q}”` : ""}
          </span>
          <button className="btn-primary ml-auto" onClick={openQuickAdd}>
            + Add
          </button>
        </div>
        {view === "inbox" && <p className="text-xs text-stone-500">New captures land here. Give a task a category or project and it moves out of the inbox.</p>}
        <div className="flex flex-wrap gap-2 text-sm">
          <select className="input w-auto" value={view} onChange={(e) => setParam("view", e.target.value === "active" ? "" : e.target.value)}>
            {Object.entries(VIEWS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
          <select className="input w-auto" value={category} onChange={(e) => setParam("category", e.target.value)}>
            <option value="">All categories</option>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
          <select className="input w-auto" value={project} onChange={(e) => setParam("project", e.target.value)}>
            <option value="">All projects</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <select className="input w-auto" value={priority} onChange={(e) => setParam("priority", e.target.value)}>
            <option value="">Any priority</option>
            {PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {PRIORITY_LABEL[p]}
              </option>
            ))}
          </select>
          <select className="input w-auto" value={maxDur} onChange={(e) => setParam("max_duration", e.target.value)}>
            <option value="">Any length</option>
            <option value="15">15 min or less</option>
            <option value="30">30 min or less</option>
            <option value="60">1 hour or less</option>
            <option value="120">2 hours or less</option>
          </select>
          <select className="input w-auto" value={sort} onChange={(e) => setParam("sort", e.target.value === "smart" ? "" : e.target.value)}>
            <option value="smart">Sort: smart</option>
            <option value="deadline">Sort: deadline</option>
            <option value="priority">Sort: priority</option>
            <option value="duration">Sort: longest first</option>
            <option value="created">Sort: newest</option>
            <option value="title">Sort: title</option>
          </select>
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        {items.map((t) => (
          <TaskRow key={t.id} task={t} />
        ))}
        {!loading && data && items.length === 0 && <div className="p-10 text-center text-sm text-stone-500">No tasks here.</div>}
      </div>
    </div>
  );
}

export default function TasksPage() {
  return (
    <Shell>
      <Suspense>
        <TaskList />
      </Suspense>
    </Shell>
  );
}
