"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, bus } from "@/lib/api";
import type { AppNotification } from "@/lib/types";

const KIND_ICON: Record<string, string> = {
  deadline_missed: "⚠",
  deadline_risk: "◔",
  overload: "▤",
  sync_failed: "⟳",
  block_start: "▸",
  test: "✓",
};

function ago(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// -- browser push --------------------------------------------------------------------------------------------------

function b64ToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const pad = "=".repeat((4 - (b64.length % 4)) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

export function pushSupported() {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window && window.isSecureContext;
}

export async function currentPushSubscription(): Promise<PushSubscription | null> {
  if (!pushSupported()) return null;
  const reg = await navigator.serviceWorker.getRegistration("/sw.js");
  return reg ? reg.pushManager.getSubscription() : null;
}

/** Ask permission, subscribe this browser and register it with the server. */
export async function enablePush(publicKey: string): Promise<void> {
  if (!pushSupported()) throw new Error("This browser can't receive push notifications here (it needs HTTPS or localhost).");
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("Notifications are blocked for this site in the browser's settings.");
  const reg = await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  const sub = (await reg.pushManager.getSubscription()) ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(publicKey) }));
  await api("/push/subscribe", { method: "POST", json: sub.toJSON() });
}

export async function disablePush(): Promise<void> {
  const sub = await currentPushSubscription();
  if (!sub) return;
  await api("/push/unsubscribe", { method: "POST", json: { endpoint: sub.endpoint } });
  await sub.unsubscribe();
}

// -- bell ----------------------------------------------------------------------------------------------------------

export function NotificationBell({ openTask }: { openTask: (id: string) => void }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const seen = useRef<Set<string> | null>(null);
  const box = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ items: AppNotification[]; unread: number }>("/notifications?limit=20");
      setItems(r.items);
      setUnread(r.unread);
      // Without push, show new ones as system notifications while the app is open (if allowed).
      if (seen.current && typeof Notification !== "undefined" && Notification.permission === "granted" && !(await currentPushSubscription())) {
        for (const n of r.items) {
          if (!seen.current.has(n.id) && !n.read_at) new Notification(n.title, { body: n.body, tag: n.id, icon: "/icon.svg" });
        }
      }
      seen.current = new Set(r.items.map((n) => n.id));
    } catch {}
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 60_000);
    const onVis = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVis);
    const off = bus.on("schedule", load);
    return () => {
      clearInterval(t);
      document.removeEventListener("visibilitychange", onVis);
      off();
    };
  }, [load]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => box.current && !box.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  async function go(n: AppNotification) {
    if (!n.read_at) {
      await api(`/notifications/${n.id}/read`, { method: "POST" }).catch(() => {});
      load();
    }
    setOpen(false);
    if (n.task_id) openTask(n.task_id);
    else if (n.link) router.push(n.link);
  }

  return (
    <div className="relative" ref={box}>
      <button
        className="relative rounded-md p-1.5 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800"
        aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}
        title="Notifications"
        onClick={() => (setOpen(!open), !open && load())}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
          <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" />
          <path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
        </svg>
        {unread > 0 && (
          <span className="absolute -right-0.5 -top-0.5 min-w-4 rounded-full bg-red-600 px-1 text-center text-[10px] font-semibold leading-4 text-white">{unread > 9 ? "9+" : unread}</span>
        )}
      </button>
      {open && (
        <div className="fixed inset-x-2 top-12 z-50 max-h-[70vh] overflow-y-auto rounded-lg border border-stone-200 bg-white shadow-lg sm:absolute sm:inset-x-auto sm:right-0 sm:top-9 sm:w-96 dark:border-stone-700 dark:bg-stone-900">
          <div className="flex items-center gap-2 border-b border-stone-100 px-3 py-2 dark:border-stone-800">
            <span className="text-sm font-semibold">Notifications</span>
            {unread > 0 && (
              <button
                className="ml-auto text-xs text-brand-700 hover:underline dark:text-brand-100"
                onClick={async () => {
                  await api("/notifications/read-all", { method: "POST" });
                  load();
                }}
              >
                Mark all read
              </button>
            )}
            <button className={`text-xs text-stone-500 hover:underline ${unread > 0 ? "" : "ml-auto"}`} onClick={() => (setOpen(false), router.push("/settings#notifications"))}>
              Settings
            </button>
          </div>
          {items.length === 0 && <div className="px-3 py-6 text-center text-sm text-stone-500">Nothing yet. Deadline risks and sync problems show up here.</div>}
          <ul>
            {items.map((n) => (
              <li key={n.id}>
                <button className={`flex w-full gap-2 px-3 py-2 text-left hover:bg-stone-50 dark:hover:bg-stone-800 ${n.read_at ? "opacity-70" : ""}`} onClick={() => go(n)}>
                  <span className={`mt-0.5 w-4 shrink-0 text-center ${n.kind === "deadline_missed" || n.kind === "sync_failed" ? "text-red-600" : "text-stone-500"}`} aria-hidden>
                    {KIND_ICON[n.kind] ?? "•"}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className={`block text-sm ${n.read_at ? "" : "font-medium"}`}>{n.title}</span>
                    {n.body && <span className="line-clamp-2 block text-xs text-stone-500">{n.body}</span>}
                    <span className="block text-[11px] text-stone-400">{ago(n.created_at)}</span>
                  </span>
                  {!n.read_at && <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-brand-600" aria-label="unread" />}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
