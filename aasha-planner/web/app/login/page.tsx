"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";

function Login() {
  const sp = useSearchParams();
  const [cfg, setCfg] = useState<{ google: boolean; dev_login: boolean } | null>(null);
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(sp.get("error"));
  useEffect(() => {
    api<{ google: boolean; dev_login: boolean }>("/auth/config").then(setCfg).catch(() => setError("The server is not reachable."));
  }, []);
  const tz = typeof Intl !== "undefined" ? Intl.DateTimeFormat().resolvedOptions().timeZone : "";

  return (
    <div className="flex min-h-dvh items-center justify-center p-4">
      <div className="w-full max-w-sm rounded-2xl border border-stone-200 bg-white p-8 shadow-sm dark:border-stone-800 dark:bg-stone-900">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/icon.svg" alt="" className="mb-4 h-10 w-10" />
        <h1 className="text-xl font-semibold">Aasha Planner</h1>
        <p className="mt-1 text-sm text-stone-500">Your tasks, deadlines and calendars, planned for you.</p>
        {cfg?.google && (
          <a href={`/api/auth/google/login?mode=signin&tz=${encodeURIComponent(tz)}`} className="btn-primary mt-6 w-full justify-center py-2">
            Continue with Google
          </a>
        )}
        {cfg && !cfg.google && !cfg.dev_login && <p className="mt-6 text-sm text-amber-700">Google sign-in is not configured on the server yet.</p>}
        {cfg?.dev_login && (
          <form
            className="mt-6 space-y-2 border-t border-stone-100 pt-4 dark:border-stone-800"
            onSubmit={async (e) => {
              e.preventDefault();
              try {
                await api("/auth/dev-login", { method: "POST", json: { email, name: email.split("@")[0], timezone: tz } });
                location.href = "/";
              } catch (err) {
                setError((err as Error).message);
              }
            }}
          >
            <div className="text-xs text-stone-500">Local development sign-in (no Google)</div>
            <input className="input" type="email" required placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
            <button className="btn w-full justify-center">Sign in locally</button>
          </form>
        )}
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
        <p className="mt-6 text-[11px] leading-relaxed text-stone-400">
          We ask for read access to your calendars and permission to add events, so we can plan around your appointments and (later) place task blocks on a calendar you choose. Read-only calendars are never changed.
        </p>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <Login />
    </Suspense>
  );
}
