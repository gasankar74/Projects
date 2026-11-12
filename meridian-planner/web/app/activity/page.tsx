"use client";

import { useCallback, useEffect, useState } from "react";
import { Shell, useApp } from "@/components/Shell";
import { api, useApi } from "@/lib/api";
import type { ActivityItem, SessionInfo } from "@/lib/types";

const KINDS = [
  { key: "all", label: "Everything" },
  { key: "account", label: "Account" },
  { key: "tasks", label: "Task changes" },
  { key: "ai", label: "By the assistant" },
];

function when(iso: string) {
  return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function device(ua: string) {
  if (!ua) return "Unknown device";
  const browser = /Edg\//.test(ua) ? "Edge" : /Chrome\//.test(ua) ? "Chrome" : /Firefox\//.test(ua) ? "Firefox" : /Safari\//.test(ua) ? "Safari" : "Browser";
  const os = /iPhone|iPad/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /Mac OS X/.test(ua) ? "macOS" : /Windows/.test(ua) ? "Windows" : /Linux/.test(ua) ? "Linux" : "";
  return os ? `${browser} on ${os}` : browser;
}

function detailText(i: ActivityItem): string {
  const d = i.detail ?? {};
  if (i.source === "task") return (d.changed as string) ? `changed ${d.changed}` : "";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(d)) {
    if (k === "user_agent") parts.push(device(String(v)));
    else if (k === "changed" && Array.isArray(v)) parts.push(v.map((x) => String(x).replace(/_/g, " ")).join(", "));
    else if (["email", "file", "created", "tasks", "skipped", "method", "ai_opt_in"].includes(k)) parts.push(`${k.replace(/_/g, " ")}: ${String(v)}`);
  }
  return parts.join(" · ");
}

function Sessions() {
  const { data, reload } = useApi<SessionInfo[]>("/auth/sessions");
  const [msg, setMsg] = useState<string | null>(null);
  if (!data) return null;
  const others = data.filter((s) => !s.current).length;
  return (
    <section className="rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center gap-2 border-b border-stone-100 px-4 py-3 dark:border-stone-800">
        <h2 className="text-sm font-semibold">Signed-in sessions</h2>
        {others > 0 && (
          <button
            className="btn ml-auto text-xs"
            onClick={async () => {
              await api("/auth/sessions/revoke-others", { method: "POST" });
              setMsg("Signed out everywhere else.");
              reload();
            }}
          >
            Sign out all others
          </button>
        )}
      </div>
      <ul className="divide-y divide-stone-100 text-sm dark:divide-stone-800">
        {data.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center gap-2 px-4 py-2">
            <span className="font-medium">{device(s.user_agent)}</span>
            {s.current && <span className="chip bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">This device</span>}
            <span className="text-xs text-stone-500">signed in {when(s.created_at)}</span>
            {!s.current && (
              <button
                className="ml-auto text-xs text-stone-500 hover:text-red-600"
                onClick={async () => {
                  await api(`/auth/sessions/${s.id}`, { method: "DELETE" });
                  reload();
                }}
              >
                Sign out
              </button>
            )}
          </li>
        ))}
      </ul>
      {msg && <div className="px-4 pb-3 text-sm">{msg}</div>}
    </section>
  );
}

function Activity() {
  const { openTask } = useApp();
  const [kind, setKind] = useState("all");
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (before: string | null) => {
      setLoading(true);
      try {
        const q = new URLSearchParams({ kind, limit: "50" });
        if (before) q.set("before", before);
        const r = await api<{ items: ActivityItem[]; next_before: string | null }>(`/activity?${q}`);
        setItems((cur) => (before ? [...cur, ...r.items] : r.items));
        setNext(r.next_before);
      } finally {
        setLoading(false);
      }
    },
    [kind],
  );
  useEffect(() => {
    load(null);
  }, [load]);

  return (
    <section className="rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
      <div className="flex flex-wrap items-center gap-1 border-b border-stone-100 px-3 py-2 dark:border-stone-800">
        <h2 className="mr-2 px-1 text-sm font-semibold">Activity</h2>
        {KINDS.map((k) => (
          <button
            key={k.key}
            className={`rounded-md px-2 py-1 text-xs ${kind === k.key ? "bg-brand-50 font-medium text-brand-700 dark:bg-brand-700/20 dark:text-brand-100" : "text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800"}`}
            onClick={() => setKind(k.key)}
          >
            {k.label}
          </button>
        ))}
      </div>
      <ol className="divide-y divide-stone-100 dark:divide-stone-800">
        {items.map((i) => (
          <li key={i.id} className="grid grid-cols-[7.5rem_1fr] gap-2 px-4 py-2 text-sm sm:grid-cols-[10rem_6rem_1fr]">
            <span className="text-xs text-stone-500 tabular-nums">{when(i.at)}</span>
            <span className={`hidden text-xs sm:block ${i.actor === "Assistant" ? "font-medium text-brand-700 dark:text-brand-100" : "text-stone-500"}`}>{i.actor}</span>
            <span className="min-w-0">
              <span className="font-medium">{i.label}</span>
              {i.task_title && (
                <>
                  {" "}
                  <button className="text-brand-700 hover:underline dark:text-brand-100" onClick={() => openTask(i.task_id!)}>
                    {i.task_title}
                  </button>
                </>
              )}
              {detailText(i) && <span className="block truncate text-xs text-stone-500">{detailText(i)}</span>}
            </span>
          </li>
        ))}
      </ol>
      {items.length === 0 && !loading && <div className="px-4 py-6 text-center text-sm text-stone-500">Nothing recorded yet.</div>}
      {next && (
        <div className="px-4 py-3">
          <button className="btn text-xs" disabled={loading} onClick={() => load(next)}>
            {loading ? "Loading…" : "Show older"}
          </button>
        </div>
      )}
    </section>
  );
}

export default function ActivityPage() {
  return (
    <Shell>
      <div className="h-full overflow-y-auto">
        <div className="mx-auto max-w-4xl space-y-4 p-4">
          <h1 className="text-lg font-semibold">Activity log</h1>
          <p className="text-sm text-stone-500">Every change to your tasks (by you, the assistant or the scheduler) and every account event: sign-ins, calendar connections, exports and imports, settings.</p>
          <Sessions />
          <Activity />
        </div>
      </div>
    </Shell>
  );
}
