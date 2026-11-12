"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { createContext, Suspense, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, bus, useApi } from "@/lib/api";
import type { Category, GoogleAccount, Project, User } from "@/lib/types";
import { QuickAdd } from "./QuickAdd";
import { TaskDrawer } from "./TaskDrawer";

interface Ctx {
  me: User | undefined;
  categories: Category[];
  projects: Project[];
  reloadMeta: () => void;
  openTask: (id: string | null) => void;
  openQuickAdd: () => void;
}

const AppCtx = createContext<Ctx | null>(null);
export function useApp(): Ctx {
  const c = useContext(AppCtx);
  if (!c) throw new Error("useApp outside Shell");
  return c;
}

function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function SyncStatus() {
  const { data: accounts, reload } = useApi<GoogleAccount[]>("/google-accounts");
  const [busy, setBusy] = useState(false);
  useEffect(() => bus.on("calendars", reload), [reload]);
  // Google push notifications (when set up) and the worker keep calendars fresh; as a fallback the app also
  // re-syncs accounts that haven't synced for a couple of minutes when the tab regains focus.
  const sync = useCallback(async () => {
    setBusy(true);
    try {
      await api("/calendars/sync?if_older_than=120", { method: "POST" });
      bus.emit("calendars");
    } catch {}
    setBusy(false);
  }, []);
  useEffect(() => {
    const onFocus = () => document.visibilityState === "visible" && sync();
    document.addEventListener("visibilitychange", onFocus);
    const t = setInterval(sync, 10 * 60 * 1000);
    return () => {
      document.removeEventListener("visibilitychange", onFocus);
      clearInterval(t);
    };
  }, [sync]);

  if (!accounts) return null;
  let tone = "bg-emerald-500";
  let text = `Calendars synced ${timeAgo(accounts.map((a) => a.last_synced_at).sort().at(0) ?? null)}`;
  if (accounts.length === 0) {
    tone = "bg-stone-400";
    text = "No calendar connected";
  } else if (accounts.some((a) => a.status === "needs_reconnect")) {
    tone = "bg-red-500";
    text = "Reconnect a calendar";
  } else if (accounts.some((a) => a.status === "error")) {
    tone = "bg-amber-500";
    text = "Calendar sync problem";
  }
  if (busy) {
    tone = "bg-sky-500 animate-pulse";
    text = "Syncing calendars…";
  }
  return (
    <Link href="/settings" className="hidden items-center gap-2 rounded-md px-2 py-1 text-xs text-stone-500 hover:bg-stone-100 sm:flex dark:hover:bg-stone-800" title="Calendar sync status">
      <span className={`h-2 w-2 rounded-full ${tone}`} />
      {text}
    </Link>
  );
}

function ThemeToggle() {
  const [dark, setDark] = useState(false);
  useEffect(() => setDark(document.documentElement.classList.contains("dark")), []);
  return (
    <button
      className="rounded-md p-1.5 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800"
      title="Toggle dark mode"
      onClick={() => {
        const next = !dark;
        setDark(next);
        document.documentElement.classList.toggle("dark", next);
        try {
          localStorage.setItem("meridian-theme", next ? "dark" : "light");
        } catch {}
      }}
    >
      {dark ? "☀" : "☾"}
    </button>
  );
}

function NavLink({ href, label, count, active, dot }: { href: string; label: string; count?: number; active: boolean; dot?: string }) {
  return (
    <Link
      href={href}
      className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-sm ${active ? "bg-brand-50 font-medium text-brand-700 dark:bg-brand-700/20 dark:text-brand-100" : "text-stone-700 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800"}`}
    >
      {dot && <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: dot }} />}
      <span className="truncate">{label}</span>
      {count ? <span className="ml-auto text-xs text-stone-400">{count}</span> : null}
    </Link>
  );
}

function Sidebar({ onNavigate }: { onNavigate: () => void }) {
  const { categories, projects, openQuickAdd, reloadMeta } = useApp();
  const path = usePathname();
  const sp = useSearchParams();
  const view = sp.get("view");
  const cat = sp.get("category");
  const proj = sp.get("project");
  const [newProject, setNewProject] = useState("");
  const { data: inbox, reload } = useApi<{ total: number }>("/tasks?view=inbox&limit=1");
  const { data: overdue, reload: reloadOverdue } = useApi<{ total: number }>("/tasks?view=overdue&limit=1");
  useEffect(() => bus.on("tasks", () => (reload(), reloadOverdue())), [reload, reloadOverdue]);

  return (
    <nav className="flex h-full flex-col gap-4 overflow-y-auto p-3" onClick={(e) => (e.target as HTMLElement).closest("a") && onNavigate()}>
      <Link href="/" className="flex items-center gap-2 px-2 pt-1">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/icon.svg" alt="" className="h-7 w-7" />
        <span className="text-base font-semibold tracking-tight">Meridian Planner</span>
      </Link>
      <button className="btn-primary justify-center" onClick={openQuickAdd} title="Quick add (Q)">
        + Add task <kbd className="ml-1 rounded bg-white/20 px-1 text-[10px]">Q</kbd>
      </button>
      <div className="space-y-0.5">
        <NavLink href="/" label="Planner" active={path === "/"} />
        <NavLink href="/tasks" label="All tasks" active={path === "/tasks" && !view && !cat && !proj} />
        <NavLink href="/matrix" label="Priority matrix" active={path === "/matrix"} />
        <NavLink href="/repeating" label="Repeating tasks" active={path === "/repeating"} />
        <NavLink href="/tasks?view=inbox" label="Inbox" count={inbox?.total} active={view === "inbox"} />
        <NavLink href="/tasks?view=overdue" label="Overdue" count={overdue?.total} active={view === "overdue"} />
        <NavLink href="/tasks?view=history" label="Completed" active={view === "history"} />
        <NavLink href="/settings" label="Calendars & settings" active={path.startsWith("/settings")} />
      </div>
      <div>
        <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-stone-400">Categories</div>
        {categories.map((c) => (
          <NavLink key={c.id} href={`/tasks?category=${c.id}`} label={c.name} count={c.open_count} dot={c.color} active={cat === c.id} />
        ))}
      </div>
      <div>
        <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-stone-400">Projects</div>
        {projects.map((p) => (
          <NavLink key={p.id} href={`/tasks?project=${p.id}`} label={p.name} count={p.open_count} active={proj === p.id} />
        ))}
        <form
          className="mt-1 px-1"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!newProject.trim()) return;
            await api("/projects", { method: "POST", json: { name: newProject.trim() } });
            setNewProject("");
            reloadMeta();
          }}
        >
          <input className="input text-xs" placeholder="+ New project" value={newProject} onChange={(e) => setNewProject(e.target.value)} />
        </form>
      </div>
    </nav>
  );
}

function ShellInner({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { data: me } = useApi<User>("/auth/me");
  const { data: categories, reload: reloadCats } = useApi<Category[]>("/categories");
  const { data: projects, reload: reloadProjects } = useApi<Project[]>("/projects");
  const [taskId, setTaskId] = useState<string | null>(null);
  const [quick, setQuick] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const reloadMeta = useCallback(() => {
    reloadCats();
    reloadProjects();
  }, [reloadCats, reloadProjects]);
  useEffect(() => bus.on("tasks", reloadMeta), [reloadMeta]);

  // Keep the stored time zone in step with the device (travel / time-zone changes).
  useEffect(() => {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (me && tz && me.timezone !== tz && !sessionStorage.getItem("meridian-tz-asked")) {
      sessionStorage.setItem("meridian-tz-asked", "1");
      if (confirm(`Your device is in ${tz}, but your planner uses ${me.timezone}. Switch to ${tz}?`)) {
        api("/auth/me", { method: "PATCH", json: { timezone: tz } }).then(() => location.reload());
      }
    }
  }, [me]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable]")) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "q" || e.key === "n") {
        e.preventDefault();
        setQuick(true);
      } else if (e.key === "/") {
        e.preventDefault();
        searchRef.current?.focus();
      } else if (e.key === "g") {
        router.push("/");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);

  const ctx: Ctx = {
    me,
    categories: categories ?? [],
    projects: projects ?? [],
    reloadMeta,
    openTask: setTaskId,
    openQuickAdd: () => setQuick(true),
  };

  return (
    <AppCtx.Provider value={ctx}>
      <div className="flex h-dvh">
        <aside className={`fixed inset-y-0 left-0 z-40 w-64 border-r border-stone-200 bg-white transition-transform lg:static lg:translate-x-0 dark:border-stone-800 dark:bg-stone-900 ${navOpen ? "translate-x-0" : "-translate-x-full"}`}>
          <Sidebar onNavigate={() => setNavOpen(false)} />
        </aside>
        {navOpen && <div className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={() => setNavOpen(false)} />}
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-12 shrink-0 items-center gap-2 border-b border-stone-200 bg-white px-3 dark:border-stone-800 dark:bg-stone-900">
            <button className="rounded-md p-1.5 lg:hidden" onClick={() => setNavOpen(true)} aria-label="Menu">
              ☰
            </button>
            <form
              className="max-w-md flex-1"
              onSubmit={(e) => {
                e.preventDefault();
                const q = searchRef.current?.value.trim();
                router.push(q ? `/tasks?view=all&q=${encodeURIComponent(q)}` : "/tasks");
              }}
            >
              <input ref={searchRef} className="input" placeholder="Search tasks  ( / )" />
            </form>
            <div className="ml-auto flex items-center gap-1">
              <SyncStatus />
              <ThemeToggle />
              <button
                className="rounded-md px-2 py-1 text-xs text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800"
                title={me?.email}
                onClick={async () => {
                  await api("/auth/logout", { method: "POST" });
                  location.href = "/login";
                }}
              >
                Sign out
              </button>
            </div>
          </header>
          <main className="min-h-0 flex-1">{children}</main>
        </div>
      </div>
      {quick && <QuickAdd onClose={() => setQuick(false)} />}
      {taskId && <TaskDrawer taskId={taskId} onClose={() => setTaskId(null)} />}
    </AppCtx.Provider>
  );
}

export function Shell({ children }: { children: React.ReactNode }) {
  return (
    <Suspense>
      <ShellInner>{children}</ShellInner>
    </Suspense>
  );
}
