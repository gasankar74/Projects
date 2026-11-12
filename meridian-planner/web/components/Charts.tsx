"use client";

import { fmtDuration } from "@/lib/dates";

/** Headline number with a label. */
export function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border border-stone-200 bg-white p-3 dark:border-stone-800 dark:bg-stone-900">
      <div className="text-xs text-stone-500">{label}</div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums">{value}</div>
      {sub && <div className="text-xs text-stone-500">{sub}</div>}
    </div>
  );
}

/** Ranked horizontal bars, one series. Identity colors come from the user's categories; text stays in ink colors. */
export function BarList({ rows, empty = "Nothing yet", unit = "min" }: { rows: { key: string; label: string; minutes: number; color?: string | null }[]; empty?: string; unit?: "min" | "count" }) {
  const max = Math.max(1, ...rows.map((r) => r.minutes));
  if (!rows.length) return <div className="py-3 text-sm text-stone-400">{empty}</div>;
  return (
    <ul className="space-y-1.5" role="list">
      {rows.map((r) => (
        <li key={r.key} className="grid grid-cols-[minmax(0,9rem)_1fr_auto] items-center gap-2 text-sm" title={`${r.label}: ${unit === "min" ? fmtDuration(r.minutes) : r.minutes}`}>
          <span className="flex min-w-0 items-center gap-1.5">
            {r.color && <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: r.color }} aria-hidden />}
            <span className="truncate">{r.label}</span>
          </span>
          <span className="h-3 rounded-r bg-stone-100 dark:bg-stone-800" aria-hidden>
            <span className="block h-3 rounded-r" style={{ width: `${(100 * r.minutes) / max}%`, background: r.color ?? "var(--color-brand-500)" }} />
          </span>
          <span className="text-right text-xs tabular-nums text-stone-600 dark:text-stone-300">{unit === "min" ? fmtDuration(r.minutes) : r.minutes}</span>
        </li>
      ))}
    </ul>
  );
}

/** Planned work against working time, one row per day. The track is capacity; the fill is planned; over capacity turns the label orange. */
export function CapacityRows({ days }: { days: { date: string; work_min: number | null; planned_min: number; extra?: string }[] }) {
  const max = Math.max(60, ...days.map((d) => Math.max(d.work_min ?? 0, d.planned_min)));
  return (
    <div>
      <div className="mb-2 flex gap-4 text-xs text-stone-500">
        <span className="flex items-center gap-1.5">
          <span className="h-2.5 w-4 rounded-sm bg-brand-500" aria-hidden /> Planned work
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-2.5 w-4 rounded-sm bg-stone-200 dark:bg-stone-700" aria-hidden /> Working time
        </span>
      </div>
      <ul className="space-y-1.5">
        {days.map((d) => {
          const dt = new Date(d.date + "T12:00:00");
          const over = d.work_min != null && d.planned_min > d.work_min;
          const label = `${dt.toLocaleDateString([], { weekday: "short", day: "numeric" })}: ${fmtDuration(d.planned_min)} planned of ${fmtDuration(d.work_min ?? 0)} working time${d.extra ? `, ${d.extra}` : ""}`;
          return (
            <li key={d.date} className="grid grid-cols-[3.5rem_1fr_7.5rem] items-center gap-2 text-sm" title={label}>
              <span className="text-xs text-stone-500">{dt.toLocaleDateString([], { weekday: "short", day: "numeric" })}</span>
              <span className="relative h-4" aria-label={label}>
                <span className="absolute inset-y-0 left-0 rounded-r bg-stone-200 dark:bg-stone-700" style={{ width: `${(100 * (d.work_min ?? 0)) / max}%` }} />
                <span className="absolute inset-y-1 left-0 rounded-r bg-brand-500" style={{ width: `${(100 * d.planned_min) / max}%` }} />
              </span>
              <span className={`text-right text-xs tabular-nums ${over ? "font-semibold text-orange-700 dark:text-orange-300" : "text-stone-600 dark:text-stone-300"}`}>
                {fmtDuration(d.planned_min)} / {fmtDuration(d.work_min ?? 0)}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function Card({ title, children, action, className = "" }: { title: string; children: React.ReactNode; action?: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-stone-200 bg-white p-4 dark:border-stone-800 dark:bg-stone-900 ${className}`}>
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-sm font-semibold">{title}</h2>
        {action && <div className="ml-auto">{action}</div>}
      </div>
      {children}
    </section>
  );
}
