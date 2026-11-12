"use client";

import { useEffect, useState } from "react";
import { api, useApi } from "@/lib/api";
import type { NotifyPrefs, NotifySettings } from "@/lib/types";
import { currentPushSubscription, disablePush, enablePush, pushSupported } from "./Notifications";

const HOURS = Array.from({ length: 24 }, (_, h) => h);

export function NotificationSettings() {
  const { data, reload } = useApi<NotifySettings>("/notifications/settings");
  const [here, setHere] = useState<boolean | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);

  useEffect(() => {
    currentPushSubscription().then((s) => setHere(!!s)).catch(() => setHere(false));
  }, []);

  if (!data) return null;
  const p = data.prefs;
  async function patch(body: Partial<NotifyPrefs>) {
    setMsg(null);
    try {
      await api("/notifications/settings", { method: "PATCH", json: body });
      reload();
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  return (
    <section id="notifications" className="scroll-mt-4 rounded-xl border border-stone-200 bg-white dark:border-stone-800 dark:bg-stone-900">
      <div className="flex items-center gap-2 border-b border-stone-100 px-4 py-3 dark:border-stone-800">
        <h2 className="text-sm font-semibold">Notifications</h2>
        <button
          className="btn ml-auto text-xs"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setMsg(null);
            try {
              const n = await api<{ channels: Record<string, string> }>("/notifications/test", { method: "POST" });
              const parts = Object.entries(n.channels).map(([k, v]) => `${k === "push" ? "browser" : k}: ${v.replace("_", " ")}`);
              setMsg(`Sent a test notification. In the app: shown${parts.length ? "; " + parts.join("; ") : ""}.`);
            } catch (e) {
              setMsg((e as Error).message);
            }
            setBusy(false);
          }}
        >
          Send a test
        </button>
      </div>
      <div className="space-y-5 p-4 text-sm">
        <p className="text-xs text-stone-500">
          Everything appears under the bell. Browser and email alerts never name a confidential task.
        </p>

        <div>
          <div className="mb-1 font-medium">Browser notifications</div>
          {!data.push.enabled ? (
            <p className="text-xs text-stone-500">Not set up on the server. Add MERIDIAN_VAPID_PUBLIC_KEY and MERIDIAN_VAPID_PRIVATE_KEY (generate them with python -m app.cli vapid).</p>
          ) : !pushSupported() ? (
            <p className="text-xs text-stone-500">This browser can&apos;t receive push notifications from this address. Open Meridian over HTTPS (your Tailscale address) to turn them on.</p>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              {here ? (
                <>
                  <span className="text-xs text-emerald-700 dark:text-emerald-300">On for this browser</span>
                  <button
                    className="btn text-xs"
                    onClick={async () => {
                      await disablePush();
                      setHere(false);
                      reload();
                    }}
                  >
                    Turn off here
                  </button>
                </>
              ) : (
                <button
                  className="btn-primary text-xs"
                  onClick={async () => {
                    setMsg(null);
                    try {
                      await enablePush(data.push.public_key!);
                      setHere(true);
                      reload();
                    } catch (e) {
                      setMsg((e as Error).message);
                    }
                  }}
                >
                  Turn on for this browser
                </button>
              )}
              <span className="text-xs text-stone-500">
                {data.push.subscriptions} {data.push.subscriptions === 1 ? "browser" : "browsers"} signed up
              </span>
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={p.push} onChange={(e) => patch({ push: e.target.checked })} /> Send browser notifications
            </label>
            <label className="flex items-center gap-1.5">
              Quiet from
              <input className="input !w-28" type="time" value={p.quiet_start} onChange={(e) => patch({ quiet_start: e.target.value })} aria-label="Quiet hours start" />
              to
              <input className="input !w-28" type="time" value={p.quiet_end} onChange={(e) => patch({ quiet_end: e.target.value })} aria-label="Quiet hours end" />
            </label>
            <label className="flex items-center gap-1.5">
              At most
              <input className="input !w-16" type="number" min={1} max={60} value={p.push_per_hour} onChange={(e) => patch({ push_per_hour: Math.max(1, Number(e.target.value) || 1) })} aria-label="Most per hour" />
              an hour
            </label>
          </div>
        </div>

        <div>
          <div className="mb-1 font-medium">Email</div>
          {!data.email.enabled ? (
            <p className="text-xs text-stone-500">Not set up on the server. Add the MERIDIAN_SMTP_* settings (Gmail works with an app password).</p>
          ) : (
            <div className="space-y-2">
              <label className="flex items-center gap-1.5">
                <input type="checkbox" checked={p.email_alerts} onChange={(e) => patch({ email_alerts: e.target.checked })} />
                Email me when a hard deadline can&apos;t be met or calendar sync stops (at most one of each every 6 hours)
              </label>
              <label className="flex flex-wrap items-center gap-1.5">
                <input type="checkbox" checked={p.email_digest} onChange={(e) => patch({ email_digest: e.target.checked })} />
                Daily digest at
                <select className="input !w-auto" value={p.digest_hour} onChange={(e) => patch({ digest_hour: Number(e.target.value) })} aria-label="Digest hour">
                  {HOURS.map((h) => (
                    <option key={h} value={h}>
                      {String(h).padStart(2, "0")}:00
                    </option>
                  ))}
                </select>
                <button
                  className="text-xs text-brand-700 hover:underline dark:text-brand-100"
                  onClick={async () => {
                    const d = await api<{ text: string }>("/notifications/digest/preview");
                    setPreview(preview ? null : d.text);
                  }}
                >
                  {preview ? "Hide preview" : "Preview"}
                </button>
              </label>
              <label className="flex flex-wrap items-center gap-1.5">
                Send to
                <input
                  className="input max-w-xs"
                  type="email"
                  defaultValue={p.email_to}
                  placeholder={data.email.to}
                  onBlur={(e) => e.target.value !== p.email_to && patch({ email_to: e.target.value.trim() })}
                />
              </label>
              {preview && <pre className="max-h-64 overflow-auto rounded-md bg-stone-50 p-2 text-xs whitespace-pre-wrap dark:bg-stone-800">{preview}</pre>}
            </div>
          )}
        </div>

        <div>
          <div className="mb-1 font-medium">What to send outside the app</div>
          <div className="grid gap-1 sm:grid-cols-2">
            {Object.entries(data.kinds).map(([k, label]) => (
              <label key={k} className="flex items-center gap-1.5">
                <input type="checkbox" checked={!!p.kinds[k]} onChange={(e) => patch({ kinds: { [k]: e.target.checked } })} />
                {label}
                {k === "block_start" && <span className="text-xs text-stone-500">(5 minutes before)</span>}
              </label>
            ))}
          </div>
        </div>
        {msg && <div className="text-sm">{msg}</div>}
      </div>
    </section>
  );
}
