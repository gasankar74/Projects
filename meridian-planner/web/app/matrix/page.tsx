"use client";

import { useEffect } from "react";
import { Shell, useApp } from "@/components/Shell";
import { PRIORITY_STYLE } from "@/components/TaskRow";
import { bus, useApi } from "@/lib/api";
import { fmtDeadline } from "@/lib/dates";
import { PRIORITY_LABEL, type Priority } from "@/lib/types";

interface Item {
  id: string;
  title: string;
  priority: Priority;
  deadline: string | null;
  score: number | null;
  scheduled_start: string | null;
  at_risk: boolean;
}
type Boxes = Record<"do" | "schedule" | "delegate" | "drop", Item[]>;

const BOXES: { key: keyof Boxes; title: string; hint: string; tone: string }[] = [
  { key: "do", title: "Important and urgent", hint: "Do first. Due within 2 days or at risk.", tone: "border-red-200 dark:border-red-500/30" },
  { key: "schedule", title: "Important, not urgent", hint: "Protect time for these.", tone: "border-brand-500/40" },
  { key: "delegate", title: "Urgent, less important", hint: "Hand off or keep short.", tone: "border-amber-200 dark:border-amber-500/30" },
  { key: "drop", title: "Neither", hint: "Batch, defer or drop.", tone: "border-stone-200 dark:border-stone-700" },
];

export default function MatrixPage() {
  return (
    <Shell>
      <Matrix />
    </Shell>
  );
}

function Matrix() {
  const { openTask } = useApp();
  const { data, reload } = useApi<Boxes>("/schedule/eisenhower");
  useEffect(() => bus.on("tasks", reload), [reload]);
  useEffect(() => bus.on("schedule", reload), [reload]);
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl p-4">
        <h1 className="text-lg font-semibold">Priority matrix</h1>
        <p className="mt-1 text-sm text-stone-500">Important means urgent or high priority. Urgent means due within two days, or at risk of missing its deadline.</p>
        <div className="mt-4 grid gap-4 md:grid-cols-2">
          {BOXES.map((b) => (
            <section key={b.key} className={`rounded-xl border-2 bg-white p-3 dark:bg-stone-900 ${b.tone}`}>
              <div className="flex items-baseline gap-2">
                <h2 className="text-sm font-semibold">{b.title}</h2>
                <span className="text-xs text-stone-500">{data?.[b.key].length ?? 0}</span>
              </div>
              <p className="text-xs text-stone-500">{b.hint}</p>
              <ul className="mt-2 space-y-1">
                {(data?.[b.key] ?? []).map((t) => (
                  <li key={t.id}>
                    <button className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-stone-50 dark:hover:bg-stone-800" onClick={() => openTask(t.id)}>
                      <span className={`chip ${PRIORITY_STYLE[t.priority]}`}>{PRIORITY_LABEL[t.priority]}</span>
                      <span className="min-w-0 flex-1 truncate">{t.title}</span>
                      {t.at_risk && <span className="chip bg-orange-100 text-orange-800 dark:bg-orange-500/20 dark:text-orange-200">At risk</span>}
                      <span className="shrink-0 text-xs text-stone-500">{t.deadline ? `Due ${fmtDeadline(t.deadline)}` : t.scheduled_start ? `▸ ${fmtDeadline(t.scheduled_start)}` : ""}</span>
                    </button>
                  </li>
                ))}
                {data && data[b.key].length === 0 && <li className="px-2 py-1 text-xs text-stone-400">Nothing here.</li>}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
