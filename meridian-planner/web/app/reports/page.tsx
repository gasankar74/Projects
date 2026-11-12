"use client";

import { useEffect, useState } from "react";
import { BarList, Card, Stat } from "@/components/Charts";
import { Shell, useApp } from "@/components/Shell";
import { api, bus, useApi } from "@/lib/api";
import { fmtDuration } from "@/lib/dates";

interface Row {
  key: string;
  label: string;
  color: string | null;
  minutes: number;
}
interface Summary {
  start: string;
  end: string;
  tracked_total_min: number;
  tracked_by_category: Row[];
  tracked_by_project: Row[];
  tracked_by_day: Row[];
  tracked_by_task: Row[];
  completed: number;
  completed_by_day: { date: string; count: number }[];
  estimates: { task_id: string; title: string; category: string | null; estimate_min: number; actual_min: number; ratio: number | null }[];
}
interface Estimates {
  categories: { category_id: string | null; category: string; color: string | null; samples: number; median_ratio: number; factor: number | null }[];
  overall_ratio: number | null;
  min_samples: number;
  enabled: boolean;
}

function iso(d: Date) {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const RANGES: { key: string; label: string; make: () => [Date, Date] }[] = [
  { key: "week", label: "This week", make: () => { const d = new Date(); const s = new Date(d); s.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return [s, d]; } },
  { key: "7", label: "Last 7 days", make: () => { const d = new Date(); const s = new Date(d); s.setDate(d.getDate() - 6); return [s, d]; } },
  { key: "30", label: "Last 30 days", make: () => { const d = new Date(); const s = new Date(d); s.setDate(d.getDate() - 29); return [s, d]; } },
  { key: "month", label: "This month", make: () => { const d = new Date(); return [new Date(d.getFullYear(), d.getMonth(), 1), d]; } },
  { key: "lastmonth", label: "Last month", make: () => { const d = new Date(); return [new Date(d.getFullYear(), d.getMonth() - 1, 1), new Date(d.getFullYear(), d.getMonth(), 0)]; } },
];

function ratioText(r: number | null) {
  if (r == null) return "–";
  if (Math.abs(r - 1) < 0.05) return "on estimate";
  return r > 1 ? `${Math.round((r - 1) * 100)}% longer` : `${Math.round((1 - r) * 100)}% shorter`;
}

function Reports() {
  const { openTask } = useApp();
  const [range, setRange] = useState("30");
  const [s, e] = (RANGES.find((r) => r.key === range) ?? RANGES[2]).make();
  const { data, reload } = useApi<Summary>(`/reports/summary?start=${iso(s)}&end=${iso(e)}`);
  const { data: est, reload: reloadEst } = useApi<Estimates>("/reports/estimates");
  useEffect(() => bus.on("timer", reload), [reload]);
  const days = (data?.tracked_by_day ?? []).map((r) => ({ ...r, color: null }));
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold">Reports</h1>
          <select className="input ml-auto !w-auto" value={range} onChange={(e) => setRange(e.target.value)} aria-label="Range">
            {RANGES.map((r) => (
              <option key={r.key} value={r.key}>
                {r.label}
              </option>
            ))}
          </select>
          <a className="btn" href={`/api/reports/time.csv?start=${iso(s)}&end=${iso(e)}`}>
            Export time (CSV)
          </a>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Time tracked" value={fmtDuration(data?.tracked_total_min ?? 0)} />
          <Stat label="Tasks completed" value={String(data?.completed ?? 0)} />
          <Stat label="Estimates vs actual" value={ratioText(est?.overall_ratio ?? null)} sub="median, last 6 months" />
          <Stat label="Tasks with tracked time" value={String(data?.tracked_by_task.length ?? 0)} />
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <Card title="By category">
            <BarList rows={data?.tracked_by_category ?? []} empty="No time tracked in this range." />
          </Card>
          <Card title="By project">
            <BarList rows={data?.tracked_by_project ?? []} empty="No time tracked in this range." />
          </Card>
          <Card title="By day">
            <BarList rows={days} empty="No time tracked in this range." />
          </Card>
          <Card title="Most time spent">
            <BarList rows={(data?.tracked_by_task ?? []).slice(0, 10)} empty="No time tracked in this range." />
          </Card>
        </div>
        <Card
          title="How long things really take"
          action={
            est && (
              <label className="flex items-center gap-1.5 text-xs" title="When on, the scheduler plans each category's tasks with its learned factor. Your estimates are not changed.">
                <input
                  type="checkbox"
                  checked={est.enabled}
                  onChange={async (ev) => {
                    await api("/schedule/settings", { method: "PATCH", json: { learn_durations: ev.target.checked } });
                    reloadEst();
                    bus.emit("schedule");
                  }}
                />
                Plan with learned estimates
              </label>
            )
          }
        >
          <p className="mb-3 text-xs text-stone-500">
            From finished tasks with tracked time. A category needs {est?.min_samples ?? 5} finished tasks before its factor is used.
          </p>
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-stone-500">
              <tr>
                <th className="py-1">Category</th>
                <th>Tasks</th>
                <th>Typical</th>
                <th>Planning factor</th>
              </tr>
            </thead>
            <tbody>
              {(est?.categories ?? []).map((c) => (
                <tr key={c.category_id ?? "none"} className="border-t border-stone-100 dark:border-stone-800">
                  <td className="py-1.5">
                    <span className="flex items-center gap-1.5">
                      {c.color && <span className="h-2 w-2 rounded-full" style={{ background: c.color }} aria-hidden />}
                      {c.category}
                    </span>
                  </td>
                  <td>{c.samples}</td>
                  <td>{ratioText(c.median_ratio)}</td>
                  <td>{c.factor != null ? `×${c.factor}` : <span className="text-stone-400">needs more history</span>}</td>
                </tr>
              ))}
              {est && est.categories.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-2 text-stone-400">
                    No finished tasks with tracked time yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>
        <Card title="Estimate against actual, finished tasks in this range">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-stone-500">
              <tr>
                <th className="py-1">Task</th>
                <th>Estimate</th>
                <th>Actual</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {(data?.estimates ?? []).map((r) => (
                <tr key={r.task_id} className="border-t border-stone-100 dark:border-stone-800">
                  <td className="max-w-0 truncate py-1.5 pr-2">
                    <button className="truncate text-left hover:underline" onClick={() => openTask(r.task_id)}>
                      {r.title}
                    </button>
                  </td>
                  <td className="tabular-nums">{fmtDuration(r.estimate_min)}</td>
                  <td className="tabular-nums">{fmtDuration(r.actual_min)}</td>
                  <td className="text-xs text-stone-500">{ratioText(r.ratio)}</td>
                </tr>
              ))}
              {data && data.estimates.length === 0 && (
                <tr>
                  <td colSpan={4} className="py-2 text-stone-400">
                    Nothing finished with tracked time in this range.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>
      </div>
    </div>
  );
}

export default function ReportsPage() {
  return (
    <Shell>
      <Reports />
    </Shell>
  );
}
