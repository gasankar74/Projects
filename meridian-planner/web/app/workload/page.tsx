"use client";

import { useEffect, useState } from "react";
import { Card } from "@/components/Charts";
import { Shell } from "@/components/Shell";
import { bus, useApi } from "@/lib/api";
import { fmtDuration } from "@/lib/dates";

interface Workload {
  start: string;
  group: string;
  groups: { key: string; label: string; color: string | null; minutes: number }[];
  days: { date: string; work_min: number; free_min: number | null; planned_min: number; by_group: Record<string, number> }[];
}

// Used only for projects that have no colour of their own, in this fixed order.
const FALLBACK = ["#1f8f86", "#6366f1", "#d97706", "#db2777", "#0284c7", "#65a30d", "#7c3aed", "#57534e"];
const NONE = "#a8a29e";

function Chart({ data }: { data: Workload }) {
  const colors = new Map<string, string>();
  let k = 0;
  for (const g of data.groups) colors.set(g.key, g.key === "none" ? NONE : (g.color ?? FALLBACK[k++ % FALLBACK.length]));
  const max = Math.max(60, ...data.days.map((d) => Math.max(d.work_min, d.planned_min)));
  return (
    <div>
      <ul className="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-stone-600 dark:text-stone-300" aria-label="Legend">
        {data.groups.map((g) => (
          <li key={g.key} className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: colors.get(g.key) }} aria-hidden />
            {g.label} <span className="text-stone-400">{fmtDuration(g.minutes)}</span>
          </li>
        ))}
        <li className="flex items-center gap-1.5">
          <span className="h-2.5 w-4 rounded-sm border border-dashed border-stone-400" aria-hidden /> Working time
        </li>
      </ul>
      <ul className="space-y-1.5">
        {data.days.map((d) => {
          const dt = new Date(d.date + "T12:00:00");
          const weekend = dt.getDay() === 0 || dt.getDay() === 6;
          const over = d.planned_min > d.work_min && d.work_min > 0;
          const segs = data.groups.filter((g) => d.by_group[g.key]);
          const tip = `${dt.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })}: ${fmtDuration(d.planned_min)} planned of ${fmtDuration(d.work_min)}${segs.length ? " — " + segs.map((g) => `${g.label} ${fmtDuration(d.by_group[g.key])}`).join(", ") : ""}`;
          return (
            <li key={d.date} className="grid grid-cols-[4.5rem_1fr_7.5rem] items-center gap-2 text-sm" title={tip}>
              <span className={`text-xs ${weekend ? "text-stone-400" : "text-stone-600 dark:text-stone-300"}`}>{dt.toLocaleDateString([], { weekday: "short", day: "numeric" })}</span>
              <span className="relative h-5" aria-label={tip}>
                <span className="absolute inset-y-0 left-0 rounded-r border border-dashed border-stone-300 dark:border-stone-600" style={{ width: `${(100 * d.work_min) / max}%` }} />
                <span className="absolute inset-y-1 right-0 left-0 flex gap-0.5">
                  {segs.map((g, i) => (
                    <span
                      key={g.key}
                      className={i === segs.length - 1 ? "rounded-r" : ""}
                      style={{ width: `${(100 * d.by_group[g.key]) / max}%`, minWidth: 2, background: colors.get(g.key) }}
                    />
                  ))}
                </span>
              </span>
              <span className={`text-right text-xs tabular-nums ${over ? "font-semibold text-orange-700 dark:text-orange-300" : "text-stone-600 dark:text-stone-300"}`}>
                {fmtDuration(d.planned_min)} / {fmtDuration(d.work_min)}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function WorkloadView() {
  const [group, setGroup] = useState<"category" | "project">("category");
  const [days, setDays] = useState(14);
  const { data, reload } = useApi<Workload>(`/workload?days=${days}&group=${group}`);
  useEffect(() => bus.on("schedule", reload), [reload]);
  useEffect(() => bus.on("tasks", reload), [reload]);
  const table = data?.days ?? [];
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold">Workload</h1>
          <div className="ml-auto flex gap-2">
            <select className="input !w-auto" value={group} onChange={(e) => setGroup(e.target.value as "category" | "project")} aria-label="Group by">
              <option value="category">By category</option>
              <option value="project">By project</option>
            </select>
            <select className="input !w-auto" value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Range">
              <option value={7}>Next 7 days</option>
              <option value={14}>Next 14 days</option>
              <option value={28}>Next 4 weeks</option>
            </select>
          </div>
        </div>
        <Card title="Planned work against working time">
          {data ? <Chart data={data} /> : <div className="text-sm text-stone-400">Loading…</div>}
        </Card>
        <details className="rounded-xl border border-stone-200 bg-white p-4 text-sm dark:border-stone-800 dark:bg-stone-900">
          <summary className="cursor-pointer font-semibold">Table view</summary>
          <table className="mt-3 w-full text-left text-xs">
            <thead className="text-stone-500">
              <tr>
                <th className="py-1">Day</th>
                <th>Working time</th>
                <th>Planned</th>
                <th>Free</th>
              </tr>
            </thead>
            <tbody>
              {table.map((d) => (
                <tr key={d.date} className="border-t border-stone-100 dark:border-stone-800">
                  <td className="py-1">{new Date(d.date + "T12:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })}</td>
                  <td>{fmtDuration(d.work_min)}</td>
                  <td>{fmtDuration(d.planned_min)}</td>
                  <td>{d.free_min != null ? fmtDuration(d.free_min) : "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </div>
    </div>
  );
}

export default function WorkloadPage() {
  return (
    <Shell>
      <WorkloadView />
    </Shell>
  );
}
