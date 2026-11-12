"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { NotificationSettings } from "@/components/NotificationSettings";
import { Shell, useApp } from "@/components/Shell";
import { api, bus, useApi } from "@/lib/api";
import type { AiStatus, Calendar, GoogleAccount, Profile as SchedProfile, ScheduleSettings, WriteStatus } from "@/lib/types";
import { PRIORITIES, PRIORITY_LABEL } from "@/lib/types";

const ROLE_LABEL: Record<string, string> = { owner: "Owner", writer: "Can edit", reader: "Read-only", freeBusyReader: "Free/busy only" };

function Card({ title, children, actions }: { title: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section className="rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center gap-2 border-b border-stone-100 px-4 py-3 dark:border-stone-800">
        <h2 className="text-sm font-semibold">{title}</h2>
        <div className="ml-auto flex gap-2">{actions}</div>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Calendars() {
  const { data: accounts, reload: reloadAccounts } = useApi<GoogleAccount[]>("/google-accounts");
  const { data: cals, reload: reloadCals } = useApi<Calendar[]>("/calendars");
  const [syncing, setSyncing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => bus.on("calendars", () => (reloadAccounts(), reloadCals())), [reloadAccounts, reloadCals]);

  async function patch(c: Calendar, body: Partial<Calendar>) {
    setMsg(null);
    try {
      await api(`/calendars/${c.id}`, { method: "PATCH", json: body });
      reloadCals();
      bus.emit("calendars");
    } catch (e) {
      setMsg((e as Error).message);
    }
  }
  async function syncNow() {
    setSyncing(true);
    try {
      const r = await api<{ results: { ok: boolean; account: string; error?: string }[] }>("/calendars/sync", { method: "POST" });
      const bad = r.results.filter((x) => !x.ok);
      setMsg(bad.length ? `Sync problem: ${bad.map((b) => `${b.account}${b.error ? ` (${b.error})` : ""}`).join(", ")}` : "All calendars are up to date.");
    } catch (e) {
      setMsg((e as Error).message);
    }
    setSyncing(false);
    bus.emit("calendars");
  }

  return (
    <Card
      title="Google Calendars"
      actions={
        <>
          <button className="btn" onClick={syncNow} disabled={syncing || !accounts?.length}>
            {syncing ? "Syncing…" : "Sync now"}
          </button>
          <a className="btn-primary" href="/api/auth/google/login?mode=connect">
            + Connect Google account
          </a>
        </>
      }
    >
      {msg && <div className="mb-3 rounded-md bg-stone-100 px-3 py-2 text-sm dark:bg-stone-800">{msg}</div>}
      {accounts?.length === 0 && <p className="text-sm text-stone-500">No Google account connected yet. Connect personal, business and family accounts; the planner treats busy time on all of them as unavailable.</p>}
      <div className="space-y-5">
        {(accounts ?? []).map((a) => (
          <div key={a.id}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-medium">{a.email}</span>
              <span
                className={`chip ${a.status === "ok" ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-200" : a.status === "needs_reconnect" ? "bg-red-100 text-red-700 dark:bg-red-500/20 dark:text-red-200" : "bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-200"}`}
              >
                {a.status === "ok" ? "Connected" : a.status === "needs_reconnect" ? "Needs reconnect" : a.status === "syncing" ? "Syncing" : "Sync error"}
              </span>
              <span className="text-xs text-stone-400">{a.last_synced_at ? `Last synced ${new Date(a.last_synced_at).toLocaleString()}` : "Not synced yet"}</span>
              {a.status === "needs_reconnect" && (
                <a className="text-xs text-brand-600 underline" href="/api/auth/google/login?mode=connect">
                  Reconnect
                </a>
              )}
              <button
                className="ml-auto text-xs text-stone-400 hover:text-red-600"
                onClick={async () => {
                  if (confirm(`Disconnect ${a.email}? Its calendars stop being read. Nothing is deleted in Google.`)) {
                    await api(`/google-accounts/${a.id}`, { method: "DELETE" });
                    bus.emit("calendars");
                  }
                }}
              >
                Disconnect
              </button>
            </div>
            {a.last_error && <div className="mt-1 text-xs text-red-600">{a.last_error}</div>}
            <table className="mt-2 w-full text-sm">
              <thead>
                <tr className="text-left text-[11px] uppercase tracking-wide text-stone-400">
                  <th className="py-1 font-medium">Calendar</th>
                  <th className="font-medium">Access</th>
                  <th className="text-center font-medium">Show</th>
                  <th className="text-center font-medium">Blocks my time</th>
                  <th className="text-center font-medium" title="Scheduled task blocks are written here">
                    Task calendar
                  </th>
                  <th className="text-center font-medium" title="Google tells Meridian about changes right away (otherwise checked every 10 minutes)">
                    Live
                  </th>
                </tr>
              </thead>
              <tbody>
                {(cals ?? [])
                  .filter((c) => c.google_account_id === a.id)
                  .map((c) => (
                    <tr key={c.id} className="border-t border-stone-100 dark:border-stone-800">
                      <td className="py-1.5">
                        <span className="mr-2 inline-block h-2.5 w-2.5 rounded-full align-middle" style={{ background: c.color ?? "#999" }} />
                        {c.summary}
                        {c.primary && <span className="ml-1 text-xs text-stone-400">(primary)</span>}
                        {c.last_error && <div className="text-xs text-red-600">{c.last_error}</div>}
                      </td>
                      <td className="text-xs text-stone-500">{ROLE_LABEL[c.access_role] ?? c.access_role}</td>
                      <td className="text-center">
                        <input type="checkbox" checked={c.selected} onChange={(e) => patch(c, { selected: e.target.checked })} />
                      </td>
                      <td className="text-center">
                        <input type="checkbox" checked={c.blocks_time} disabled={!c.selected} onChange={(e) => patch(c, { blocks_time: e.target.checked })} />
                      </td>
                      <td className="text-center">
                        {c.writable ? <input type="radio" name="task-target" checked={c.is_task_target} onChange={() => patch(c, { is_task_target: true })} /> : <span className="text-xs text-stone-400" title="Read-only calendars are never written to">—</span>}
                      </td>
                      <td className="text-center text-xs" title={c.push_enabled ? "Push notifications on" : "Checked every 10 minutes"}>
                        {c.push_enabled ? <span className="text-emerald-600">●</span> : <span className="text-stone-300 dark:text-stone-600">○</span>}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
      {(cals ?? []).length > 0 && <TaskCalendarStatus cals={cals ?? []} onOff={(c) => patch(c, { is_task_target: false })} />}
    </Card>
  );
}

function TaskCalendarStatus({ cals, onOff }: { cals: Calendar[]; onOff: (c: Calendar) => void }) {
  const { data, reload } = useApi<WriteStatus>("/calendars/write-status");
  const [busy, setBusy] = useState(false);
  useEffect(() => bus.on("calendars", reload), [reload]);
  useEffect(() => bus.on("schedule", reload), [reload]);
  const target = cals.find((c) => c.is_task_target);
  return (
    <div className="mt-4 rounded-lg bg-stone-50 p-3 text-sm dark:bg-stone-800/50">
      {target ? (
        <div className="flex flex-wrap items-center gap-2">
          <span>
            Planned blocks for the next {""}
            <WriteDays /> are written to <b>{target.summary}</b>.
          </span>
          {data && (
            <span className="text-xs text-stone-500">
              {data.synced} on your calendar{data.pending ? ` · ${data.pending} waiting to be written` : ""}
              {data.errors ? ` · ${data.errors} problem${data.errors > 1 ? "s" : ""}` : ""}
            </span>
          )}
          <span className="ml-auto flex gap-2">
            <button
              className="btn"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await api("/calendars/write-now", { method: "POST" });
                } finally {
                  setBusy(false);
                  reload();
                }
              }}
            >
              {busy ? "Writing…" : "Write now"}
            </button>
            <button className="btn" onClick={() => onOff(target)}>
              Stop writing blocks
            </button>
          </span>
          {data?.last_error && <div className="w-full text-xs text-red-600">{data.last_error}</div>}
          <p className="w-full text-xs text-stone-500">
            Confidential tasks appear as “Focus block”, marked private. If you drag one of these events in Google Calendar, Meridian locks the task at the new time.
          </p>
        </div>
      ) : (
        <span className="text-stone-500">Pick a task calendar above to see your planned work in Google Calendar too. Read-only calendars are never written to.</span>
      )}
    </div>
  );
}

function WriteDays() {
  const { data, setData } = useApi<ScheduleSettings>("/schedule/settings");
  if (!data) return <>days</>;
  return (
    <select
      aria-label="Days written ahead"
      className="rounded border border-stone-300 bg-transparent px-1 text-sm dark:border-stone-700"
      value={data.write_days}
      onChange={async (e) => {
        const out = await api<ScheduleSettings>("/schedule/settings", { method: "PATCH", json: { write_days: Number(e.target.value) } });
        setData(out);
      }}
    >
      {[3, 7, 14, 30].map((n) => (
        <option key={n} value={n}>
          {n} days
        </option>
      ))}
    </select>
  );
}

function Profile() {
  const { me } = useApp();
  const [tz, setTz] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => setTz(me?.timezone ?? ""), [me]);
  const zones: string[] = typeof Intl !== "undefined" && "supportedValuesOf" in Intl ? (Intl as unknown as { supportedValuesOf: (k: string) => string[] }).supportedValuesOf("timeZone") : [];
  return (
    <Card title="Profile">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <span className="label">Signed in as</span>
          <div className="text-sm">{me?.email}</div>
        </div>
        <label>
          <span className="label">Planning time zone</span>
          <div className="flex gap-2">
            <select className="input" value={tz} onChange={(e) => (setTz(e.target.value), setSaved(false))}>
              {[tz, ...zones.filter((z) => z !== tz)].map((z) => (
                <option key={z}>{z}</option>
              ))}
            </select>
            <button
              className="btn"
              onClick={async () => {
                await api("/auth/me", { method: "PATCH", json: { timezone: tz } });
                setSaved(true);
              }}
            >
              {saved ? "Saved" : "Save"}
            </button>
          </div>
        </label>
      </div>
    </Card>
  );
}

function Categories() {
  const { categories, reloadMeta } = useApp();
  const { data: profiles } = useApi<SchedProfile[]>("/profiles");
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const save = async (id: string, body: object) => {
    setErr(null);
    try {
      await api(`/categories/${id}`, { method: "PATCH", json: body });
      reloadMeta();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <Card title="Categories">
      <p className="mb-3 text-xs text-stone-500">Each category has a default priority and can use a scheduling profile's hours (for example, Family tasks only in the evening).</p>
      <div className="space-y-1.5">
        {categories.map((c) => (
          <div key={c.id} className="flex items-center gap-2">
            <input type="color" className="h-7 w-8 cursor-pointer rounded border-0 bg-transparent" defaultValue={c.color} onBlur={(e) => e.target.value !== c.color && save(c.id, { color: e.target.value })} />
            <input className="input" defaultValue={c.name} onBlur={(e) => e.target.value.trim() && e.target.value !== c.name && save(c.id, { name: e.target.value.trim() })} />
            <select className="input w-40" aria-label={`${c.name} hours`} value={c.scheduling_profile_id ?? ""} onChange={(e) => save(c.id, { scheduling_profile_id: e.target.value || null })}>
              <option value="">Work hours</option>
              {(profiles ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
            <select
              className="input w-36"
              aria-label={`${c.name} AI access`}
              title="What the AI assistant may read about tasks in this category"
              value={c.ai_access}
              onChange={(e) => save(c.id, { ai_access: e.target.value })}
            >
              <option value="default">AI: usual</option>
              <option value="hidden">AI: hide all</option>
              <option value="allowed">AI: may read all</option>
            </select>
            <select className="input w-32" value={c.default_priority} onChange={(e) => save(c.id, { default_priority: e.target.value })}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {PRIORITY_LABEL[p]}
                </option>
              ))}
            </select>
            <button
              className="text-xs text-stone-400 hover:text-red-600"
              onClick={async () => {
                if (confirm(`Delete “${c.name}”? Its ${c.open_count} open tasks keep existing without a category.`)) {
                  await api(`/categories/${c.id}`, { method: "DELETE" });
                  reloadMeta();
                }
              }}
            >
              Delete
            </button>
          </div>
        ))}
      </div>
      <form
        className="mt-3 flex gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!name.trim()) return;
          try {
            await api("/categories", { method: "POST", json: { name: name.trim(), position: categories.length } });
            setName("");
            reloadMeta();
          } catch (e2) {
            setErr((e2 as Error).message);
          }
        }}
      >
        <input className="input" placeholder="New category" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn">Add</button>
      </form>
      {err && <div className="mt-2 text-xs text-red-600">{err}</div>}
    </Card>
  );
}

function Data() {
  const { me } = useApp();
  const [msg, setMsg] = useState<string | null>(null);
  return (
    <Card title="Your data">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Link className="btn-primary" href="/import">
          Import from Motion (CSV)
        </Link>
        <Link className="btn" href="/activity">
          Activity log and sessions
        </Link>
      </div>
      <div className="flex flex-wrap gap-2">
        <a className="btn" href="/api/export/json">
          Export everything (JSON)
        </a>
        <a className="btn" href="/api/export/tasks.csv">
          Export tasks (CSV)
        </a>
        <label className="btn cursor-pointer">
          Import JSON export
          <input
            type="file"
            accept="application/json"
            className="hidden"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (!f) return;
              try {
                const r = await api<{ created: number; skipped_duplicates: number }>("/import/json", { method: "POST", body: await f.text(), headers: { "Content-Type": "application/json" } });
                setMsg(`Imported ${r.created} tasks; skipped ${r.skipped_duplicates} that already exist.`);
                bus.emit("tasks");
              } catch (err) {
                setMsg((err as Error).message);
              }
              e.target.value = "";
            }}
          />
        </label>
      </div>
      {msg && <div className="mt-2 text-sm">{msg}</div>}
      <div className="mt-6 border-t border-stone-100 pt-4 dark:border-stone-800">
        <button
          className="text-sm text-red-600 hover:underline"
          onClick={async () => {
            const typed = prompt(
              `This permanently deletes your account, tasks and calendar connections. Task blocks Meridian added to your Google Calendar are removed and its Google access is revoked. Your own calendar events are not touched.\n\nType your email (${me?.email}) to confirm.`,
            );
            if (!typed) return;
            try {
              await api("/account/delete", { method: "POST", json: { confirm_email: typed } });
              location.href = "/login";
            } catch (err) {
              setMsg((err as Error).message);
            }
          }}
        >
          Delete my account
        </button>
      </div>
    </Card>
  );
}

const DAYS: [string, string][] = [
  ["mon", "Monday"],
  ["tue", "Tuesday"],
  ["wed", "Wednesday"],
  ["thu", "Thursday"],
  ["fri", "Friday"],
  ["sat", "Saturday"],
  ["sun", "Sunday"],
];

function HoursEditor({ hours, onChange }: { hours: Record<string, [string, string][]>; onChange: (h: Record<string, [string, string][]>) => void }) {
  const setDay = (d: string, spans: [string, string][]) => onChange({ ...hours, [d]: spans });
  return (
    <div className="space-y-2">
        {DAYS.map(([d, label]) => {
          const spans = hours[d] ?? [];
          const on = spans.length > 0;
          return (
            <div key={d} className="flex flex-wrap items-center gap-2 text-sm">
              <label className="flex w-32 items-center gap-2">
                <input type="checkbox" checked={on} onChange={(e) => setDay(d, e.target.checked ? [["09:00", "17:00"]] : [])} />
                {label}
              </label>
              {spans.map((sp, i) => (
                <span key={i} className="flex items-center gap-1">
                  <input aria-label={`${label} start`} type="time" className="input !w-32" value={sp[0]} onChange={(e) => setDay(d, spans.map((x, j) => (j === i ? [e.target.value, x[1]] : x)))} />
                  <span>to</span>
                  <input aria-label={`${label} end`} type="time" className="input !w-32" value={sp[1] === "24:00" ? "23:59" : sp[1]} onChange={(e) => setDay(d, spans.map((x, j) => (j === i ? [x[0], e.target.value] : x)))} />
                  {spans.length > 1 && (
                    <button className="text-stone-400 hover:text-red-600" aria-label="Remove hours" onClick={() => setDay(d, spans.filter((_, j) => j !== i))}>
                      ×
                    </button>
                  )}
                </span>
              ))}
              {on && (
                <button className="text-xs text-brand-600 hover:underline" onClick={() => setDay(d, [...spans, ["18:00", "20:00"]])}>
                  + add hours
                </button>
              )}
              {!on && <span className="text-xs text-stone-400">No scheduled work</span>}
            </div>
          );
        })}
      </div>
  );
}

function Scheduling() {
  const { data, setData } = useApi<ScheduleSettings>("/schedule/settings");
  const [msg, setMsg] = useState<string | null>(null);
  const [draft, setDraft] = useState<ScheduleSettings | null>(null);
  useEffect(() => {
    if (data) setDraft(data);
  }, [data]);
  if (!draft) return <Card title="Work hours and scheduling">Loading…</Card>;

  const hours = draft.work_hours;
  async function save() {
    setMsg(null);
    try {
      const out = await api<ScheduleSettings>("/schedule/settings", {
        method: "PATCH",
        json: {
          work_hours: draft!.work_hours,
          transition_min: draft!.transition_min,
          daily_cap_min: draft!.daily_cap_min,
          horizon_days: draft!.horizon_days,
          default_min_session: draft!.default_min_session,
          energy_hours: draft!.energy_hours,
          optimize_hours: draft!.optimize_hours,
          holidays: draft!.holidays,
          event_travel_min: draft!.event_travel_min,
        },
      });
      setData(out);
      setMsg("Saved. Your plan is being updated.");
      bus.emit("schedule");
    } catch (e) {
      setMsg((e as Error).message);
    }
  }
  return (
    <Card title="Work hours and scheduling" actions={<button className="btn-primary" onClick={save}>Save</button>}>
      <p className="mb-3 text-xs text-stone-500">
        Tasks are only scheduled inside these hours, in your time zone ({draft.timezone}). Calendar events still block time inside them.
      </p>
      <HoursEditor hours={hours} onChange={(h) => setDraft({ ...draft, work_hours: h })} />
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="text-sm">
          <span className="label">Break between tasks (minutes)</span>
          <input className="input" type="number" min={0} max={120} value={draft.transition_min} onChange={(e) => setDraft({ ...draft, transition_min: Number(e.target.value) })} />
        </label>
        <label className="text-sm">
          <span className="label">Most task time per day (minutes, empty = no limit)</span>
          <input className="input" type="number" min={15} value={draft.daily_cap_min ?? ""} onChange={(e) => setDraft({ ...draft, daily_cap_min: e.target.value ? Number(e.target.value) : null })} />
        </label>
        <label className="text-sm">
          <span className="label">Shortest work session when splitting (minutes)</span>
          <input className="input" type="number" min={15} value={draft.default_min_session} onChange={(e) => setDraft({ ...draft, default_min_session: Number(e.target.value) })} />
        </label>
        <label className="text-sm">
          <span className="label">Plan this many days ahead</span>
          <input className="input" type="number" min={7} max={180} value={draft.horizon_days} onChange={(e) => setDraft({ ...draft, horizon_days: Number(e.target.value) })} />
        </label>
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        {(["high", "low"] as const).map((lvl) => {
          const v = draft.energy_hours?.[lvl];
          return (
            <div key={lvl} className="text-sm">
              <span className="label">{lvl === "high" ? "High-energy work goes first into" : "Low-energy work goes first into"}</span>
              <div className="flex items-center gap-1">
                <input type="checkbox" aria-label={`Use ${lvl}-energy hours`} checked={!!v} onChange={(e) => setDraft({ ...draft, energy_hours: { ...draft.energy_hours, [lvl]: e.target.checked ? (lvl === "high" ? ["09:00", "12:00"] : ["13:00", "16:00"]) : undefined } })} />
                {v ? (
                  <>
                    <input type="time" className="input !w-32" aria-label={`${lvl} energy start`} value={v[0]} onChange={(e) => setDraft({ ...draft, energy_hours: { ...draft.energy_hours, [lvl]: [e.target.value, v[1]] } })} />
                    <span>to</span>
                    <input type="time" className="input !w-32" aria-label={`${lvl} energy end`} value={v[1]} onChange={(e) => setDraft({ ...draft, energy_hours: { ...draft.energy_hours, [lvl]: [v[0], e.target.value] } })} />
                  </>
                ) : (
                  <span className="text-xs text-stone-400">no preference</span>
                )}
              </div>
            </div>
          );
        })}
        <label className="text-sm">
          <span className="label">Travel time around events with an address (minutes each way)</span>
          <input className="input" type="number" min={0} max={240} value={draft.event_travel_min} onChange={(e) => setDraft({ ...draft, event_travel_min: Number(e.target.value) })} />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={draft.optimize_hours > 0} onChange={(e) => setDraft({ ...draft, optimize_hours: e.target.checked ? 48 : 0 })} />
          <span>
            Fine-tune the next two days with the optimiser
            <span className="block text-xs text-stone-500">Rearranges near-term work to honour preferences and finish important work sooner, never breaking a rule.</span>
          </span>
        </label>
        <div className="text-sm sm:col-span-2">
          <span className="label">Holidays (no work hours; repeating tasks can skip them)</span>
          <div className="flex flex-wrap items-center gap-1">
            {draft.holidays.map((h) => (
              <span key={h} className="chip bg-stone-100 dark:bg-stone-800">
                {new Date(h + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}
                <button className="ml-1 text-stone-400 hover:text-red-600" aria-label={`Remove ${h}`} onClick={() => setDraft({ ...draft, holidays: draft.holidays.filter((x) => x !== h) })}>
                  ×
                </button>
              </span>
            ))}
            <input type="date" aria-label="Add holiday" className="input !w-40" onChange={(e) => e.target.value && setDraft({ ...draft, holidays: [...new Set([...draft.holidays, e.target.value])].sort() })} />
          </div>
        </div>
      </div>
      {msg && <div className="mt-3 text-xs text-stone-600 dark:text-stone-300">{msg}</div>}
    </Card>
  );
}

const EVENINGS_WEEKENDS: Record<string, [string, string][]> = {
  mon: [["18:00", "21:00"]],
  tue: [["18:00", "21:00"]],
  wed: [["18:00", "21:00"]],
  thu: [["18:00", "21:00"]],
  fri: [["18:00", "21:00"]],
  sat: [["09:00", "17:00"]],
  sun: [["10:00", "16:00"]],
};

function Profiles() {
  const { data: profiles, reload } = useApi<SchedProfile[]>("/profiles");
  const [open, setOpen] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, SchedProfile>>({});
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const draftOf = (p: SchedProfile) => drafts[p.id] ?? p;
  async function save(p: SchedProfile) {
    setErr(null);
    try {
      const d = draftOf(p);
      await api(`/profiles/${p.id}`, { method: "PATCH", json: { name: d.name, work_hours: d.work_hours, daily_cap_min: d.daily_cap_min } });
      setDrafts(({ [p.id]: _, ...rest }) => rest);
      reload();
      bus.emit("schedule");
    } catch (e) {
      setErr((e as Error).message);
    }
  }
  return (
    <Card title="Scheduling profiles">
      <p className="mb-3 text-xs text-stone-500">
        Named sets of hours, for work that belongs outside (or inside part of) your normal work hours. Give a task or a whole category a profile and it is only planned in those hours.
      </p>
      <div className="space-y-2">
        {(profiles ?? []).map((p) => {
          const d = draftOf(p);
          const changed = !!drafts[p.id];
          return (
            <div key={p.id} className="rounded-lg border border-stone-200 dark:border-stone-800">
              <div className="flex flex-wrap items-center gap-2 p-2">
                <button className="text-sm font-medium" onClick={() => setOpen(open === p.id ? null : p.id)} aria-expanded={open === p.id}>
                  {open === p.id ? "▾" : "▸"} {p.name}
                </button>
                <span className="text-xs text-stone-500">
                  {Object.values(p.work_hours).filter((x) => x.length).length} days{p.daily_cap_min ? ` · at most ${p.daily_cap_min} min a day` : ""}
                </span>
                <span className="ml-auto flex gap-2">
                  {changed && (
                    <button className="btn-primary" onClick={() => save(p)}>
                      Save
                    </button>
                  )}
                  <button
                    className="text-xs text-stone-400 hover:text-red-600"
                    onClick={async () => {
                      if (confirm(`Delete the “${p.name}” profile? Its tasks go back to their category's hours or your work hours.`)) {
                        await api(`/profiles/${p.id}`, { method: "DELETE" });
                        reload();
                      }
                    }}
                  >
                    Delete
                  </button>
                </span>
              </div>
              {open === p.id && (
                <div className="space-y-3 border-t border-stone-100 p-3 dark:border-stone-800">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="text-sm">
                      <span className="label">Name</span>
                      <input className="input" value={d.name} onChange={(e) => setDrafts({ ...drafts, [p.id]: { ...d, name: e.target.value } })} />
                    </label>
                    <label className="text-sm">
                      <span className="label">Most time per day (minutes, empty = no limit)</span>
                      <input className="input" type="number" min={15} value={d.daily_cap_min ?? ""} onChange={(e) => setDrafts({ ...drafts, [p.id]: { ...d, daily_cap_min: e.target.value ? Number(e.target.value) : null } })} />
                    </label>
                  </div>
                  <HoursEditor hours={d.work_hours} onChange={(h) => setDrafts({ ...drafts, [p.id]: { ...d, work_hours: h } })} />
                </div>
              )}
            </div>
          );
        })}
      </div>
      <form
        className="mt-3 flex gap-2"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!name.trim()) return;
          setErr(null);
          try {
            const p = await api<SchedProfile>("/profiles", { method: "POST", json: { name: name.trim(), work_hours: EVENINGS_WEEKENDS } });
            setName("");
            reload();
            setOpen(p.id);
          } catch (e2) {
            setErr((e2 as Error).message);
          }
        }}
      >
        <input className="input" placeholder="New profile, e.g. Personal" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn">Add</button>
      </form>
      {err && <div className="mt-2 text-xs text-red-600">{err}</div>}
    </Card>
  );
}

function Assistant() {
  const { me } = useApp();
  const { data: st } = useApi<AiStatus>("/assistant/status");
  const [optIn, setOptIn] = useState(false);
  useEffect(() => setOptIn(!!me?.ai_opt_in), [me]);
  return (
    <Card title="AI assistant">
      {st && (
        <div className="mb-3 flex items-center gap-2 text-sm">
          <span className={`h-2 w-2 rounded-full ${st.ok ? "bg-emerald-500" : "bg-red-500"}`} />
          {st.provider === "none" ? "Switched off on this server" : `${st.model} (${st.provider}${st.local ? ", on your own server" : ", external provider"})`}
          {st.error && <span className="text-xs text-red-600">{st.error}</span>}
        </div>
      )}
      <p className="mb-2 text-xs text-stone-500">
        The assistant changes tasks through the same rules as the app, and the scheduler decides times. Deleting, changing more than five tasks, or moving work to another day always waits for your
        Confirm. Confidential tasks reach it only as “Confidential task” with their timing.
        {st && !st.local && " With an external provider, calendar event titles are hidden too unless you allow everything below."}
      </p>
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-1"
          checked={optIn}
          onChange={async (e) => {
            const v = e.target.checked;
            if (v && !confirm("Let the assistant read the details of confidential tasks too (unless a category or project hides them)?")) return;
            setOptIn(v);
            await api("/auth/me", { method: "PATCH", json: { ai_opt_in: v } });
          }}
        />
        <span>
          Allow the assistant to read confidential tasks
          <span className="block text-xs text-stone-500">Off by default. You can also allow single tasks in the task panel, or set a category or project to hide or allow everything.</span>
        </span>
      </label>
    </Card>
  );
}

export default function SettingsPage() {
  return (
    <Shell>
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 p-4">
          <h1 className="text-lg font-semibold">Calendars & settings</h1>
          <Calendars />
          <Scheduling />
          <Profiles />
          <Profile />
          <Assistant />
          <NotificationSettings />
          <Categories />
          <Data />
        </div>
      </div>
    </Shell>
  );
}
