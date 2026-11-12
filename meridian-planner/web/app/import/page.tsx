"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Shell, useApp } from "@/components/Shell";
import { api, bus, useApi } from "@/lib/api";
import { fmtDuration } from "@/lib/dates";
import type { ImportJob, ImportRow, ImportRowState } from "@/lib/types";

const STATE: Record<ImportRowState, { label: string; tone: string }> = {
  new: { label: "New", tone: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200" },
  duplicate: { label: "Already in Meridian", tone: "bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200" },
  duplicate_in_file: { label: "Repeated in file", tone: "bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200" },
  already_imported: { label: "Imported before", tone: "bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300" },
  completed: { label: "Completed", tone: "bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-200" },
  error: { label: "Can't import", tone: "bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-200" },
};

const FILTERS: { key: string; label: string; test: (r: ImportRow) => boolean }[] = [
  { key: "all", label: "All", test: () => true },
  { key: "import", label: "Will import", test: (r) => r.action === "import" },
  { key: "dupes", label: "Duplicates", test: (r) => r.state === "duplicate" || r.state === "duplicate_in_file" || r.state === "already_imported" },
  { key: "done", label: "Completed", test: (r) => r.state === "completed" },
  { key: "problems", label: "Problems", test: (r) => r.state === "error" || r.warnings.length > 0 },
];

function fmtWhen(iso: string | null | undefined) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function Upload({ onJob }: { onJob: (j: ImportJob) => void }) {
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  const { data: past } = useApi<{ id: string; filename: string; status: string; created_at: string; result: ImportJob["result"] }[]>("/imports");

  async function send(f: File) {
    setErr(null);
    if (f.size > 5_000_000) return setErr("That file is larger than 5 MB.");
    setBusy(true);
    try {
      onJob(await api<ImportJob>("/imports", { method: "POST", json: { filename: f.name, text: await f.text(), source: "motion_csv" } }));
    } catch (e) {
      setErr((e as Error).message);
    }
    setBusy(false);
  }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-stone-200 bg-white p-4 text-sm dark:border-stone-800 dark:bg-stone-900">
        <p>
          Export your tasks from Motion as a CSV file, then drop it here. Any CSV with a title column works too. You&apos;ll see every row before anything is saved, and tasks that are already
          in Meridian are skipped.
        </p>
        <label
          className={`mt-4 flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-10 text-center ${drag ? "border-brand-500 bg-brand-50 dark:bg-brand-700/10" : "border-stone-300 dark:border-stone-700"}`}
          onDragOver={(e) => (e.preventDefault(), setDrag(true))}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files?.[0];
            if (f) send(f);
          }}
        >
          <span className="font-medium">{busy ? "Reading…" : "Drop a CSV file here, or choose one"}</span>
          <span className="mt-1 text-xs text-stone-500">Up to 5 MB and 5,000 rows</span>
          <input type="file" accept=".csv,text/csv,text/plain" className="hidden" onChange={(e) => e.target.files?.[0] && send(e.target.files[0])} disabled={busy} />
        </label>
        {err && <div className="mt-2 text-sm text-red-600">{err}</div>}
      </div>
      {past && past.length > 0 && (
        <div className="rounded-xl border border-stone-200 bg-white p-4 text-sm dark:border-stone-800 dark:bg-stone-900">
          <div className="mb-2 font-semibold">Earlier imports</div>
          <ul className="divide-y divide-stone-100 dark:divide-stone-800">
            {past.map((j) => (
              <li key={j.id} className="flex flex-wrap items-center gap-2 py-1.5">
                <span className="font-medium">{j.filename || "CSV"}</span>
                <span className="text-xs text-stone-500">{fmtWhen(j.created_at)}</span>
                <span className="ml-auto text-xs text-stone-500">
                  {j.status === "applied" ? `${j.result?.created ?? 0} tasks created` : j.status === "cancelled" ? "Cancelled" : <Link href={`/import?id=${j.id}`} className="text-brand-700 underline dark:text-brand-100">Continue review</Link>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function Review({ job, setJob, onDone }: { job: ImportJob; setJob: (j: ImportJob) => void; onDone: (j: ImportJob) => void }) {
  const { openTask, reloadMeta } = useApp();
  const [filter, setFilter] = useState("all");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const rows = job.rows ?? [];
  const s = job.summary!;
  const shown = useMemo(() => rows.filter((FILTERS.find((f) => f.key === filter) ?? FILTERS[0]).test), [rows, filter]);

  async function patch(body: object) {
    setErr(null);
    try {
      setJob(await api<ImportJob>(`/imports/${job.id}`, { method: "PATCH", json: body }));
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-stone-200 bg-white p-4 dark:border-stone-800 dark:bg-stone-900">
        <div className="flex flex-wrap items-baseline gap-2">
          <h2 className="font-semibold">{job.filename || "CSV file"}</h2>
          <span className="text-sm text-stone-500">{s.rows} rows</span>
        </div>
        <div className="mt-3 flex flex-wrap gap-2 text-xs">
          <span className="chip bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">{s.to_import} to import</span>
          {s.duplicates > 0 && <span className="chip bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">{s.duplicates} duplicates skipped</span>}
          {s.already_imported > 0 && <span className="chip bg-stone-100 text-stone-600 dark:bg-stone-800 dark:text-stone-300">{s.already_imported} imported before</span>}
          {s.completed > 0 && <span className="chip bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-200">{s.completed} completed</span>}
          {s.errors > 0 && <span className="chip bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-200">{s.errors} can&apos;t import</span>}
        </div>
        {(s.new_projects.length > 0 || s.unknown_categories.length > 0) && (
          <div className="mt-3 space-y-1 text-xs text-stone-600 dark:text-stone-300">
            {s.new_projects.length > 0 && <div>New projects to create: {s.new_projects.join(", ")}</div>}
            {s.unknown_categories.length > 0 && (
              <div>
                No category called {s.unknown_categories.map((c) => `“${c}”`).join(", ")}.{" "}
                {job.options.create_categories ? "They will be created." : "Those tasks get no category unless you create them."}
              </div>
            )}
          </div>
        )}
      </div>

      <details className="rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900" open>
        <summary className="cursor-pointer px-4 py-3 text-sm font-semibold">Columns and options</summary>
        <div className="grid gap-6 px-4 pb-4 lg:grid-cols-[3fr_2fr]">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-stone-500">
              <tr>
                <th className="py-1">Column in the file</th>
                <th>Example</th>
                <th>Use as</th>
              </tr>
            </thead>
            <tbody>
              {(job.header ?? []).map((h) => (
                <tr key={h} className="border-t border-stone-100 dark:border-stone-800">
                  <td className="py-1.5 pr-2 font-medium">{h}</td>
                  <td className="max-w-[10rem] truncate pr-2 text-xs text-stone-500" title={job.samples?.[h]}>
                    {job.samples?.[h] || "—"}
                  </td>
                  <td>
                    <select className="input !py-1 text-xs" value={job.mapping?.[h] ?? "notes"} onChange={(e) => patch({ mapping: { [h]: e.target.value } })} aria-label={`Use ${h} as`}>
                      {job.targets.map((t) => (
                        <option key={t.key} value={t.key}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="space-y-3 text-sm">
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={job.options.include_completed} onChange={(e) => patch({ options: { include_completed: e.target.checked }, reset_decisions: true })} />
              Bring completed tasks into history
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={job.options.create_categories} onChange={(e) => patch({ options: { create_categories: e.target.checked } })} />
              Create categories that don&apos;t exist yet
            </label>
            <label className="flex items-center gap-2">
              When a row has no duration, use
              <select className="input !w-auto !py-1" value={job.options.default_duration} onChange={(e) => patch({ options: { default_duration: Number(e.target.value) } })}>
                {[15, 30, 45, 60, 90, 120].map((m) => (
                  <option key={m} value={m}>
                    {fmtDuration(m)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2">
              Deadlines are
              <select className="input !w-auto !py-1" value={job.options.deadline_type} onChange={(e) => patch({ options: { deadline_type: e.target.value } })}>
                <option value="soft">soft (targets)</option>
                <option value="hard">hard (must meet)</option>
              </select>
              <span className="text-xs text-stone-500">unless the file says</span>
            </label>
            <label className="flex items-center gap-2">
              Dates are written
              <select className="input !w-auto !py-1" value={job.options.date_order} onChange={(e) => patch({ options: { date_order: e.target.value } })}>
                <option value="mdy">month/day/year</option>
                <option value="dmy">day/month/year</option>
              </select>
            </label>
          </div>
        </div>
      </details>

      <div className="rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
        <div className="flex flex-wrap gap-1 border-b border-stone-100 px-3 py-2 dark:border-stone-800">
          {FILTERS.map((f) => (
            <button key={f.key} className={`rounded-md px-2 py-1 text-xs ${filter === f.key ? "bg-brand-50 font-medium text-brand-700 dark:bg-brand-700/20 dark:text-brand-100" : "text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800"}`} onClick={() => setFilter(f.key)}>
              {f.label} <span className="text-stone-400">{rows.filter(f.test).length}</span>
            </button>
          ))}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] text-left text-sm">
            <thead className="text-xs text-stone-500">
              <tr>
                <th className="w-8 px-3 py-2" aria-label="Import" />
                <th>Task</th>
                <th>Deadline</th>
                <th>Duration</th>
                <th>Project</th>
                <th className="pr-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {shown.slice(0, 500).map((r) => (
                <tr key={r.index} className={`border-t border-stone-100 align-top dark:border-stone-800 ${r.action === "skip" ? "text-stone-400" : ""}`}>
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      aria-label={`Import row ${r.index + 1}`}
                      disabled={r.state === "error"}
                      checked={r.action === "import"}
                      onChange={(e) => patch({ decisions: { [String(r.index)]: e.target.checked ? "import" : "skip" } })}
                    />
                  </td>
                  <td className="max-w-xs py-2 pr-2">
                    <div className="truncate font-medium">{r.fields.title || <span className="italic">no title</span>}</div>
                    <div className="text-xs text-stone-500">
                      {[r.fields.priority, r.fields.category, ...(r.fields.tags ?? []).map((t) => `#${t}`)].filter(Boolean).join(" · ")}
                    </div>
                    {r.warnings.length > 0 && <div className="text-xs text-amber-700 dark:text-amber-300">{r.warnings.join("; ")}</div>}
                  </td>
                  <td className="py-2 pr-2 text-xs whitespace-nowrap">
                    {fmtWhen(r.fields.deadline)}
                    {r.fields.deadline && <span className="ml-1 text-stone-400">{r.fields.deadline_type}</span>}
                  </td>
                  <td className="py-2 pr-2 text-xs tabular-nums">{r.fields.duration_min ? fmtDuration(r.fields.duration_min) : ""}</td>
                  <td className="max-w-[10rem] truncate py-2 pr-2 text-xs">{r.fields.project}</td>
                  <td className="py-2 pr-3">
                    <span className={`chip whitespace-nowrap ${STATE[r.state].tone}`}>{STATE[r.state].label}</span>
                    {r.duplicate_of?.id && (
                      <button className="mt-0.5 block text-left text-xs text-stone-500 underline" onClick={() => openTask(r.duplicate_of!.id!)}>
                        {r.duplicate_of.deleted ? "deleted task" : "see the existing task"}
                      </button>
                    )}
                    {r.duplicate_of?.row != null && <div className="text-xs text-stone-500">same as row {r.duplicate_of.row + 1}</div>}
                    {r.error && <div className="text-xs text-red-600">{r.error}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {shown.length > 500 && <div className="px-3 py-2 text-xs text-stone-500">Showing the first 500 of {shown.length} rows.</div>}
          {shown.length === 0 && <div className="px-3 py-6 text-center text-sm text-stone-500">No rows here.</div>}
        </div>
      </div>

      <div className="sticky bottom-0 flex flex-wrap items-center gap-2 border-t border-stone-200 bg-stone-50/95 py-3 dark:border-stone-800 dark:bg-stone-950/95">
        {err && <span className="text-sm text-red-600">{err}</span>}
        <button
          className="btn ml-auto"
          onClick={async () => {
            await api(`/imports/${job.id}/cancel`, { method: "POST" });
            onDone({ ...job, status: "cancelled" });
          }}
        >
          Cancel import
        </button>
        <button
          className="btn-primary"
          disabled={busy || s.to_import === 0}
          onClick={async () => {
            setBusy(true);
            setErr(null);
            try {
              const result = await api<ImportJob["result"]>(`/imports/${job.id}/apply`, { method: "POST" });
              bus.emit("tasks");
              bus.emit("schedule");
              reloadMeta();
              onDone({ ...job, status: "applied", result });
            } catch (e) {
              setErr((e as Error).message);
            }
            setBusy(false);
          }}
        >
          {busy ? "Importing…" : `Import ${s.to_import} ${s.to_import === 1 ? "task" : "tasks"}`}
        </button>
      </div>
    </div>
  );
}

function Done({ job, again }: { job: ImportJob; again: () => void }) {
  if (job.status === "cancelled")
    return (
      <div className="rounded-xl border border-stone-200 bg-white p-6 text-sm dark:border-stone-800 dark:bg-stone-900">
        Import cancelled. Nothing was saved.{" "}
        <button className="text-brand-700 underline dark:text-brand-100" onClick={again}>
          Import another file
        </button>
      </div>
    );
  const r = job.result!;
  return (
    <div className="space-y-2 rounded-xl border border-emerald-200 bg-emerald-50/60 p-6 text-sm dark:border-emerald-500/30 dark:bg-emerald-950/20">
      <div className="text-base font-semibold">
        Imported {r.created} {r.created === 1 ? "task" : "tasks"}
      </div>
      {r.projects_created.length > 0 && <div>New projects: {r.projects_created.join(", ")}</div>}
      {r.categories_created.length > 0 && <div>New categories: {r.categories_created.join(", ")}</div>}
      <div>The planner is fitting them into your calendar now. Importing the same file again won&apos;t create duplicates.</div>
      <div className="flex gap-2 pt-2">
        <Link className="btn-primary" href="/">
          Open the planner
        </Link>
        <Link className="btn" href="/tasks">
          See all tasks
        </Link>
        <button className="btn" onClick={again}>
          Import another file
        </button>
      </div>
    </div>
  );
}

function ImportPage() {
  const router = useRouter();
  const sp = useSearchParams();
  const id = sp.get("id");
  const [job, setJob] = useState<ImportJob | null>(null);

  const load = useCallback(async () => {
    if (id) setJob(await api<ImportJob>(`/imports/${id}`).catch(() => null));
    else setJob(null);
  }, [id]);
  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl space-y-4 p-4">
        <h1 className="text-lg font-semibold">Import tasks</h1>
        {!job && <Upload onJob={(j) => (setJob(j), router.replace(`/import?id=${j.id}`))} />}
        {job?.status === "review" && <Review job={job} setJob={setJob} onDone={setJob} />}
        {job && job.status !== "review" && <Done job={job} again={() => (setJob(null), router.replace("/import"))} />}
      </div>
    </div>
  );
}

export default function Page() {
  return (
    <Shell>
      <Suspense>
        <ImportPage />
      </Suspense>
    </Shell>
  );
}
