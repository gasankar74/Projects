"use client";

import { useEffect, useState } from "react";
import { Shell, useApp } from "@/components/Shell";
import { api, bus, useApi } from "@/lib/api";
import type { Calendar, GoogleAccount } from "@/lib/types";
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
                  <th className="text-center font-medium" title="Phase 3 writes scheduled task blocks here">
                    Task calendar
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
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ))}
      </div>
    </Card>
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
      <p className="mb-3 text-xs text-stone-500">Each category gets its own default priority now, and its own scheduling hours in Phase 3.</p>
      <div className="space-y-1.5">
        {categories.map((c) => (
          <div key={c.id} className="flex items-center gap-2">
            <input type="color" className="h-7 w-8 cursor-pointer rounded border-0 bg-transparent" defaultValue={c.color} onBlur={(e) => e.target.value !== c.color && save(c.id, { color: e.target.value })} />
            <input className="input" defaultValue={c.name} onBlur={(e) => e.target.value.trim() && e.target.value !== c.name && save(c.id, { name: e.target.value.trim() })} />
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
            const typed = prompt(`This permanently deletes your account, tasks and calendar connections. Type your email (${me?.email}) to confirm.`);
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

export default function SettingsPage() {
  return (
    <Shell>
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 p-4">
          <h1 className="text-lg font-semibold">Calendars & settings</h1>
          <Calendars />
          <Profile />
          <Categories />
          <Data />
        </div>
      </div>
    </Shell>
  );
}
