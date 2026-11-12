"use client";

import { useEffect, useState } from "react";
import { api, bus, useApi } from "@/lib/api";
import { DAY_NAMES, fmtDeadline, fmtDuration, fromLocalInput, toLocalInput } from "@/lib/dates";
import type { Block, Calendar, PlanWarning, Task, TaskEvent } from "@/lib/types";
import { PRIORITIES, PRIORITY_LABEL, STATUS_LABEL } from "@/lib/types";
import { useApp } from "./Shell";

type Form = Partial<Task> & { tagsText?: string; linksText?: string };

function Field({ label, children, className = "" }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={`block ${className}`}>
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

export function TaskDrawer({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const { categories, projects } = useApp();
  const { data: task, reload, setData } = useApi<Task>(`/tasks/${taskId}`);
  const { data: history, reload: reloadHistory } = useApi<TaskEvent[]>(`/tasks/${taskId}/history`);
  const { data: calendars } = useApi<Calendar[]>("/calendars");
  const { data: others } = useApi<{ items: Task[] }>("/tasks?limit=500");
  const [form, setForm] = useState<Form>({});
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newItem, setNewItem] = useState("");
  const [tab, setTab] = useState<"details" | "schedule" | "history">("details");

  useEffect(() => {
    if (task) {
      setForm({ ...task, tagsText: task.tags.join(", "), linksText: task.links.map((l) => (l.title ? `${l.title} | ${l.url}` : l.url)).join("\n") });
      setDirty(false);
    }
  }, [task]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !(e.target as HTMLElement).closest("input, textarea, select")) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const set = (patch: Form) => {
    setForm((f) => ({ ...f, ...patch }));
    setDirty(true);
  };

  async function call(path: string, init: Parameters<typeof api>[1]) {
    setError(null);
    try {
      const t = await api<Task>(path, init);
      if (t && typeof t === "object" && "id" in t) setData(t);
      bus.emit("tasks");
      reloadHistory();
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  }

  async function save() {
    if (!task) return;
    const links = (form.linksText ?? "")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => {
        const [a, b] = l.includes("|") ? l.split("|").map((s) => s.trim()) : ["", l];
        return { title: a, url: b };
      });
    const body = {
      title: form.title,
      description: form.description || null,
      notes: form.notes || null,
      status: form.status,
      priority: form.priority,
      category_id: form.category_id || null,
      project_id: form.project_id || null,
      client: form.client || null,
      deadline: form.deadline || null,
      deadline_type: form.deadline_type,
      preferred_date: form.preferred_date || null,
      earliest_start: form.earliest_start || null,
      duration_min: Number(form.duration_min),
      remaining_min: Number(form.remaining_min),
      can_split: form.can_split,
      min_session_min: form.min_session_min ? Number(form.min_session_min) : null,
      max_session_min: form.max_session_min ? Number(form.max_session_min) : null,
      is_fixed: form.is_fixed,
      pinned_start: form.pinned_start || null,
      locked: form.locked,
      auto_schedule: form.auto_schedule,
      preferred_time_of_day: form.preferred_time_of_day || null,
      allowed_days: form.allowed_days && form.allowed_days.length < 7 ? form.allowed_days : null,
      buffer_before_min: Number(form.buffer_before_min ?? 0),
      buffer_after_min: Number(form.buffer_after_min ?? 0),
      energy: form.energy || null,
      calendar_id: form.calendar_id || null,
      tags: (form.tagsText ?? "").split(",").map((t) => t.trim()).filter(Boolean),
      links,
      confidential: form.confidential,
      ai_allowed: form.ai_allowed,
    };
    // Send only what changed, so the server's rules (e.g. leaving the inbox when filed) see real edits.
    const changed = Object.fromEntries(
      Object.entries(body).filter(([k, v]) => JSON.stringify(v ?? null) !== JSON.stringify((task as unknown as Record<string, unknown>)[k] ?? null)),
    );
    if (Object.keys(changed).length === 0) return setDirty(false);
    if (await call(`/tasks/${task.id}`, { method: "PATCH", json: changed })) setDirty(false);
  }

  const writable = (calendars ?? []).filter((c) => c.writable);
  const candidates = (others?.items ?? []).filter((t) => t.id !== taskId && !task?.depends_on.includes(t.id));

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/20" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="flex h-full w-full max-w-xl flex-col bg-white shadow-2xl dark:bg-stone-900">
        <div className="flex items-center gap-2 border-b border-stone-200 px-4 py-2.5 dark:border-stone-800">
          {task && (
            <button className={task.status === "done" ? "btn" : "btn-primary"} onClick={() => call(`/tasks/${task.id}/${task.status === "done" ? "reopen" : "complete"}`, { method: "POST" })}>
              {task.status === "done" ? "Reopen" : "✓ Complete"}
            </button>
          )}
          <div className="ml-2 flex gap-1 text-sm">
            {(["details", "schedule", "history"] as const).map((t) => (
              <button key={t} className={`rounded px-2 py-1 ${tab === t ? "bg-stone-100 font-medium dark:bg-stone-800" : "text-stone-500"}`} onClick={() => setTab(t)}>
                {t === "details" ? "Details" : t === "schedule" ? "Schedule" : "History"}
              </button>
            ))}
          </div>
          <button className="ml-auto rounded p-1 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        {!task ? (
          <div className="p-6 text-sm text-stone-500">Loading…</div>
        ) : tab === "schedule" ? (
          <WhyTab taskId={task.id} />
        ) : tab === "history" ? (
          <div className="flex-1 overflow-y-auto p-4 text-sm">
            {(history ?? []).map((h) => (
              <div key={h.id} className="border-b border-stone-100 py-2 dark:border-stone-800">
                <div className="flex justify-between text-xs text-stone-500">
                  <span className="font-medium capitalize text-stone-700 dark:text-stone-300">{h.action.replace("_", " ")}</span>
                  <span>
                    {new Date(h.at).toLocaleString()} · {h.actor}
                  </span>
                </div>
                {h.action === "updated" && (
                  <ul className="mt-1 text-xs text-stone-500">
                    {Object.entries(h.diff).map(([k, v]) => (
                      <li key={k}>
                        {k.replace(/_/g, " ")}: {JSON.stringify((v as { from: unknown }).from)} → {JSON.stringify((v as { to: unknown }).to)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="flex-1 space-y-4 overflow-y-auto p-4">
            {task.overdue && <div className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">Overdue: the deadline was {fmtDeadline(task.deadline)}.</div>}
            <input className="w-full bg-transparent text-lg font-semibold outline-none" value={form.title ?? ""} onChange={(e) => set({ title: e.target.value })} />
            <textarea className="input min-h-[70px]" placeholder="Description" value={form.description ?? ""} onChange={(e) => set({ description: e.target.value })} />

            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Field label="Status">
                <select className="input" value={form.status} onChange={(e) => set({ status: e.target.value as Task["status"] })}>
                  {Object.entries(STATUS_LABEL).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Priority">
                <select className="input" value={form.priority} onChange={(e) => set({ priority: e.target.value as Task["priority"] })}>
                  {PRIORITIES.map((p) => (
                    <option key={p} value={p}>
                      {PRIORITY_LABEL[p]}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Category">
                <select className="input" value={form.category_id ?? ""} onChange={(e) => set({ category_id: e.target.value || null })}>
                  <option value="">None (inbox)</option>
                  {categories.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Project">
                <select className="input" value={form.project_id ?? ""} onChange={(e) => set({ project_id: e.target.value || null })}>
                  <option value="">None</option>
                  {projects.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Client">
                <input className="input" value={form.client ?? ""} onChange={(e) => set({ client: e.target.value })} />
              </Field>
              <Field label="Energy needed">
                <select className="input" value={form.energy ?? ""} onChange={(e) => set({ energy: (e.target.value || null) as Task["energy"] })}>
                  <option value="">Any</option>
                  <option value="high">High</option>
                  <option value="medium">Medium</option>
                  <option value="low">Low</option>
                </select>
              </Field>
            </div>

            <section className="rounded-lg border border-stone-200 p-3 dark:border-stone-800">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-stone-400">Time and deadlines</div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                <Field label="Deadline" className="col-span-2 sm:col-span-1">
                  <input type="datetime-local" className="input" value={toLocalInput(form.deadline ?? null)} onChange={(e) => set({ deadline: fromLocalInput(e.target.value) })} />
                </Field>
                <Field label="Deadline type">
                  <select className="input" value={form.deadline_type} onChange={(e) => set({ deadline_type: e.target.value as "hard" | "soft" })}>
                    <option value="hard">Hard (must meet)</option>
                    <option value="soft">Soft (target)</option>
                  </select>
                </Field>
                <Field label="Preferred finish date">
                  <input type="date" className="input" value={form.preferred_date ?? ""} onChange={(e) => set({ preferred_date: e.target.value || null })} />
                </Field>
                <Field label="Earliest start">
                  <input type="datetime-local" className="input" value={toLocalInput(form.earliest_start ?? null)} onChange={(e) => set({ earliest_start: fromLocalInput(e.target.value) })} />
                </Field>
                <Field label="Estimated duration (min)">
                  <input type="number" min={5} step={5} className="input" value={form.duration_min ?? 30} onChange={(e) => set({ duration_min: Number(e.target.value) })} />
                </Field>
                <Field label={`Remaining (min) · ${task.completion_pct}% done`}>
                  <input type="number" min={0} step={5} className="input" value={form.remaining_min ?? 0} onChange={(e) => set({ remaining_min: Number(e.target.value) })} />
                </Field>
              </div>
            </section>

            <section className="rounded-lg border border-stone-200 p-3 dark:border-stone-800">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-stone-400">Scheduling rules</div>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={!!form.auto_schedule} onChange={(e) => set({ auto_schedule: e.target.checked })} /> Auto-schedule
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={!!form.can_split} onChange={(e) => set({ can_split: e.target.checked })} /> Can be split
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={!!form.locked} onChange={(e) => set({ locked: e.target.checked })} /> Locked in place
                </label>
                <Field label="Min session (min)">
                  <input type="number" min={5} step={5} className="input" value={form.min_session_min ?? ""} onChange={(e) => set({ min_session_min: e.target.value ? Number(e.target.value) : null })} />
                </Field>
                <Field label="Max session (min)">
                  <input type="number" min={5} step={5} className="input" value={form.max_session_min ?? ""} onChange={(e) => set({ max_session_min: e.target.value ? Number(e.target.value) : null })} />
                </Field>
                <Field label="Preferred time">
                  <select className="input" value={form.preferred_time_of_day ?? ""} onChange={(e) => set({ preferred_time_of_day: (e.target.value || null) as Task["preferred_time_of_day"] })}>
                    <option value="">Any time</option>
                    <option value="morning">Morning</option>
                    <option value="afternoon">Afternoon</option>
                    <option value="evening">Evening</option>
                  </select>
                </Field>
                <Field label="Pinned start (fixed time)">
                  <input type="datetime-local" className="input" value={toLocalInput(form.pinned_start ?? null)} onChange={(e) => set({ pinned_start: fromLocalInput(e.target.value), is_fixed: !!e.target.value })} />
                </Field>
                <Field label="Buffer before (min)">
                  <input type="number" min={0} step={5} className="input" value={form.buffer_before_min ?? 0} onChange={(e) => set({ buffer_before_min: Number(e.target.value) })} />
                </Field>
                <Field label="Buffer after (min)">
                  <input type="number" min={0} step={5} className="input" value={form.buffer_after_min ?? 0} onChange={(e) => set({ buffer_after_min: Number(e.target.value) })} />
                </Field>
                <div className="col-span-2 sm:col-span-3">
                  <span className="label">Allowed days</span>
                  <div className="flex flex-wrap gap-1">
                    {DAY_NAMES.map((d, i) => {
                      const days = form.allowed_days ?? [0, 1, 2, 3, 4, 5, 6];
                      const on = days.includes(i);
                      return (
                        <button
                          key={d}
                          type="button"
                          className={`rounded-md border px-2 py-1 text-xs ${on ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-700/30 dark:text-brand-100" : "border-stone-300 text-stone-400 dark:border-stone-700"}`}
                          onClick={() => set({ allowed_days: on ? days.filter((x) => x !== i) : [...days, i].sort() })}
                        >
                          {d}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <Field label="Write blocks to calendar" className="col-span-2 sm:col-span-3">
                  <select className="input" value={form.calendar_id ?? ""} onChange={(e) => set({ calendar_id: e.target.value || null })}>
                    <option value="">Default task calendar</option>
                    {writable.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.summary}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              {task.recurrence && (
                <div className="mt-2 text-xs text-stone-500">
                  Repeats: {task.recurrence.text ?? task.recurrence.rrule} <span className="text-stone-400">(series generation arrives in Phase 3)</span>
                </div>
              )}
            </section>

            <section>
              <div className="label">Checklist</div>
              {task.checklist.map((i) => (
                <label key={i.id} className="flex items-center gap-2 py-0.5 text-sm">
                  <input type="checkbox" checked={i.done} onChange={(e) => call(`/tasks/${task.id}/checklist/${i.id}`, { method: "PATCH", json: { done: e.target.checked } }).then(reload)} />
                  <span className={i.done ? "text-stone-400 line-through" : ""}>{i.text}</span>
                  <button className="ml-auto text-xs text-stone-400 hover:text-red-600" onClick={() => call(`/tasks/${task.id}/checklist/${i.id}`, { method: "DELETE" }).then(reload)}>
                    remove
                  </button>
                </label>
              ))}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (!newItem.trim()) return;
                  call(`/tasks/${task.id}/checklist`, { method: "POST", json: { text: newItem.trim() } }).then(reload);
                  setNewItem("");
                }}
              >
                <input className="input mt-1" placeholder="+ Add subtask" value={newItem} onChange={(e) => setNewItem(e.target.value)} />
              </form>
            </section>

            <section>
              <div className="label">Waits for (dependencies)</div>
              {task.depends_on.map((id) => {
                const t = others?.items.find((x) => x.id === id);
                return (
                  <div key={id} className="flex items-center gap-2 text-sm">
                    <span>⧗ {t?.title ?? "Completed task"}</span>
                    <button className="ml-auto text-xs text-stone-400 hover:text-red-600" onClick={() => call(`/tasks/${task.id}/dependencies/${id}`, { method: "DELETE" })}>
                      remove
                    </button>
                  </div>
                );
              })}
              <select className="input mt-1" value="" onChange={(e) => e.target.value && call(`/tasks/${task.id}/dependencies`, { method: "POST", json: { depends_on_task_id: e.target.value } })}>
                <option value="">+ This task can't start until…</option>
                {candidates.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.title}
                  </option>
                ))}
              </select>
              {task.blocking.length > 0 && <div className="mt-1 text-xs text-stone-500">{task.blocking.length} other task(s) are waiting for this one.</div>}
            </section>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Tags (comma separated)">
                <input className="input" value={form.tagsText ?? ""} onChange={(e) => set({ tagsText: e.target.value })} />
              </Field>
              <Field label="Links (one per line, “title | url”)">
                <textarea className="input min-h-[38px]" value={form.linksText ?? ""} onChange={(e) => set({ linksText: e.target.value })} />
              </Field>
            </div>
            <Field label="Notes">
              <textarea className="input min-h-[60px]" value={form.notes ?? ""} onChange={(e) => set({ notes: e.target.value })} />
            </Field>
            <div className="space-y-1 rounded-lg bg-stone-50 p-3 text-sm dark:bg-stone-800/50">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={!!form.confidential} onChange={(e) => set({ confidential: e.target.checked })} /> Confidential (masked on calendars, hidden from AI)
              </label>
              {form.confidential && (
                <label className="flex items-center gap-2 pl-5 text-xs text-stone-500">
                  <input type="checkbox" checked={!!form.ai_allowed} onChange={(e) => set({ ai_allowed: e.target.checked })} /> Allow the AI assistant to read this task's details
                </label>
              )}
            </div>
            {task.links.length > 0 && (
              <div className="text-sm">
                {task.links.map((l) => (
                  <a key={l.url} href={l.url} target="_blank" rel="noreferrer" className="mr-3 text-brand-600 underline">
                    {l.title || l.url}
                  </a>
                ))}
              </div>
            )}
            <button
              className="text-xs text-red-600 hover:underline"
              onClick={async () => {
                if (confirm("Delete this task?")) {
                  await api(`/tasks/${task.id}`, { method: "DELETE" });
                  bus.emit("tasks");
                  onClose();
                }
              }}
            >
              Delete task
            </button>
          </div>
        )}
        {tab === "details" && task && (
          <div className="flex items-center gap-2 border-t border-stone-200 px-4 py-2.5 dark:border-stone-800">
            {error && <span className="text-xs text-red-600">{error}</span>}
            <span className="ml-auto text-xs text-stone-400">{dirty ? "Unsaved changes" : "Saved"}</span>
            <button className="btn-primary" disabled={!dirty} onClick={save}>
              Save
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

interface Why {
  stats: { score: number; parts: Record<string, number>; status: string; start: string | null; finish: string | null; slack_min: number | null; at_risk: boolean; unscheduled_min: number } | null;
  blocks: Block[];
  warnings: PlanWarning[];
  auto_schedule: boolean;
  done: boolean;
}

const PART_LABEL: Record<string, string> = { P: "Priority", U: "Urgency (free time left before the deadline)", C: "Deadline type", B: "Other tasks waiting on it", J: "Project", R: "Progress already made", A: "Narrow allowed hours" };

/** What the scheduler decided for this task, and why. */
function WhyTab({ taskId }: { taskId: string }) {
  const { data, reload } = useApi<Why>(`/schedule/tasks/${taskId}/why`);
  useEffect(() => bus.on("schedule", reload), [reload]);
  useEffect(() => bus.on("tasks", reload), [reload]);
  if (!data) return <div className="p-6 text-sm text-stone-500">Loading…</div>;
  if (data.done) return <div className="p-6 text-sm text-stone-500">This task is finished, so it isn&apos;t scheduled.</div>;
  if (!data.auto_schedule) return <div className="p-6 text-sm text-stone-500">Automatic scheduling is off for this task. Turn it on in Details to have it planned.</div>;
  const st = data.stats;
  return (
    <div className="flex-1 space-y-4 overflow-y-auto p-4 text-sm">
      {data.warnings.map((w, i) => (
        <div key={i} className="rounded-lg border border-orange-200 bg-orange-50 p-2.5 text-xs text-orange-900 dark:border-orange-500/30 dark:bg-orange-500/10 dark:text-orange-100">
          {w.message}
        </div>
      ))}
      {st && (
        <section>
          <h3 className="label">Score {st.score} / 100</h3>
          <ul className="space-y-1">
            {Object.entries(st.parts).map(([k, v]) => (
              <li key={k} className="flex items-center gap-2 text-xs">
                <span className="w-56 text-stone-600 dark:text-stone-300">{PART_LABEL[k] ?? k}</span>
                <span className="h-1.5 flex-1 rounded bg-stone-100 dark:bg-stone-800">
                  <span className="block h-1.5 rounded bg-brand-500" style={{ width: `${Math.round(v * 100)}%` }} />
                </span>
                <span className="w-9 text-right text-stone-500">{Math.round(v * 100)}%</span>
              </li>
            ))}
          </ul>
          {st.finish && (
            <p className="mt-2 text-xs text-stone-500">
              Planned to finish {new Date(st.finish).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
              {st.slack_min !== null && st.slack_min >= 0 && `, with ${fmtDuration(st.slack_min)} of working time to spare before the deadline`}.
            </p>
          )}
        </section>
      )}
      <section>
        <h3 className="label">Planned sessions</h3>
        {data.blocks.length === 0 && <p className="text-xs text-stone-500">Nothing scheduled yet.</p>}
        <ul className="space-y-3">
          {data.blocks.map((b) => (
            <li key={b.id} className="rounded-lg border border-stone-200 p-2.5 dark:border-stone-800">
              <div className="flex items-center gap-2 text-xs font-medium">
                {new Date(b.start).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} – {new Date(b.end).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                {b.locked && <span className="chip bg-stone-200 dark:bg-stone-700">Locked</span>}
              </div>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-xs text-stone-600 dark:text-stone-300">
                {(b.reason.why ?? []).map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
