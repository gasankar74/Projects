"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Recurrence } from "@/lib/types";

const RR_DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ORD: [string, string][] = [
  ["1", "first"],
  ["2", "second"],
  ["3", "third"],
  ["4", "fourth"],
  ["-1", "last"],
];

type Preset = "daily" | "weekdays" | "weekly" | "biweekly" | "monthly_day" | "monthly_nth" | "last_bizday" | "first_bizday" | "quarterly" | "yearly" | "custom";

interface Parsed {
  preset: Preset;
  days: number[];
  monthDay: number;
  nth: string;
  nthDay: number;
}

function parse(rrule: string | undefined): Parsed {
  const p: Parsed = { preset: "weekly", days: [4], monthDay: 1, nth: "1", nthDay: 0 };
  if (!rrule) return p;
  const parts = Object.fromEntries(rrule.split(";").map((x) => x.split("=") as [string, string]));
  const byday = (parts.BYDAY ?? "").split(",").filter(Boolean);
  const interval = Number(parts.INTERVAL ?? 1);
  if (parts.FREQ === "DAILY" && interval === 1 && !byday.length) return { ...p, preset: "daily" };
  if (parts.FREQ === "WEEKLY" && byday.join(",") === "MO,TU,WE,TH,FR" && interval === 1) return { ...p, preset: "weekdays" };
  if (parts.FREQ === "WEEKLY" && byday.length && byday.every((d) => RR_DAYS.includes(d)) && (interval === 1 || interval === 2))
    return { ...p, preset: interval === 2 ? "biweekly" : "weekly", days: byday.map((d) => RR_DAYS.indexOf(d)) };
  if (parts.FREQ === "MONTHLY" && byday.join(",") === "MO,TU,WE,TH,FR" && parts.BYSETPOS === "-1") return { ...p, preset: "last_bizday" };
  if (parts.FREQ === "MONTHLY" && byday.join(",") === "MO,TU,WE,TH,FR" && parts.BYSETPOS === "1") return { ...p, preset: "first_bizday" };
  if (parts.FREQ === "MONTHLY" && parts.BYMONTHDAY && interval === 1) return { ...p, preset: "monthly_day", monthDay: Number(parts.BYMONTHDAY) };
  if (parts.FREQ === "MONTHLY" && byday.length === 1) {
    const m = byday[0].match(/^(-?\d)([A-Z]{2})$/);
    if (m) return { ...p, preset: "monthly_nth", nth: m[1], nthDay: RR_DAYS.indexOf(m[2]) };
  }
  if (parts.FREQ === "MONTHLY" && interval === 3 && !byday.length) return { ...p, preset: "quarterly" };
  if (parts.FREQ === "YEARLY" && interval === 1 && !byday.length) return { ...p, preset: "yearly" };
  return { ...p, preset: "custom" };
}

function build(p: Parsed, custom: string): string {
  switch (p.preset) {
    case "daily":
      return "FREQ=DAILY";
    case "weekdays":
      return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR";
    case "weekly":
      return `FREQ=WEEKLY;BYDAY=${(p.days.length ? p.days : [4]).map((d) => RR_DAYS[d]).join(",")}`;
    case "biweekly":
      return `FREQ=WEEKLY;INTERVAL=2;BYDAY=${(p.days.length ? p.days : [4]).map((d) => RR_DAYS[d]).join(",")}`;
    case "monthly_day":
      return `FREQ=MONTHLY;BYMONTHDAY=${p.monthDay}`;
    case "monthly_nth":
      return `FREQ=MONTHLY;BYDAY=${p.nth}${RR_DAYS[p.nthDay]}`;
    case "last_bizday":
      return "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1";
    case "first_bizday":
      return "FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1";
    case "quarterly":
      return "FREQ=MONTHLY;INTERVAL=3";
    case "yearly":
      return "FREQ=YEARLY";
    default:
      return custom || "FREQ=WEEKLY";
  }
}

const PRESETS: [Preset, string][] = [
  ["daily", "Every day"],
  ["weekdays", "Every weekday"],
  ["weekly", "Every week on…"],
  ["biweekly", "Every other week on…"],
  ["monthly_day", "Every month on day…"],
  ["monthly_nth", "Every month on the…"],
  ["last_bizday", "Last business day of the month"],
  ["first_bizday", "First business day of the month"],
  ["quarterly", "Every 3 months"],
  ["yearly", "Every year"],
  ["custom", "Custom rule (RRULE)"],
];

export function describeRecurrence(r: Recurrence | null | undefined): string {
  if (!r) return "";
  if (r.text) return r.text;
  if (r.kind === "after_completion") return `${r.every ?? 1} ${r.unit ?? "weeks"} after each one is done`;
  const p = parse(r.rrule);
  return PRESETS.find(([k]) => k === p.preset)?.[1] ?? r.rrule ?? "Repeats";
}

export function RepeatEditor({ value, onChange, seriesId }: { value: Recurrence | null; onChange: (v: Recurrence | null) => void; seriesId?: string | null }) {
  const mode = !value ? "none" : value.kind === "after_completion" ? "after_completion" : "fixed";
  const parsed = parse(value?.rrule);
  const [custom, setCustom] = useState(value?.rrule ?? "");
  const [preview, setPreview] = useState<{ dates: string[]; lead_days: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!value) return setPreview(null);
    const h = setTimeout(async () => {
      try {
        const rule = { ...value };
        delete rule.text;
        setPreview(await api("/series/preview", { method: "POST", json: { rule, count: 4 } }));
        setErr(null);
      } catch (e) {
        setPreview(null);
        setErr((e as Error).message);
      }
    }, 300);
    return () => clearTimeout(h);
  }, [value]);

  // Any structural edit drops the free-text description from quick add, so it can't go stale.
  const upd = (patch: Partial<Recurrence>) => onChange({ ...(value ?? {}), ...patch, text: undefined });
  const setFixed = (p: Partial<Parsed>) => {
    const next = { ...parsed, ...p };
    upd({ kind: "fixed", rrule: build(next, custom) });
  };

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <label className="block">
          <span className="label">Repeat</span>
          <select
            className="input"
            value={mode}
            onChange={(e) => {
              const m = e.target.value;
              if (m === "none") onChange(null);
              else if (m === "fixed") onChange({ kind: "fixed", rrule: "FREQ=WEEKLY;BYDAY=FR", due_time: value?.due_time ?? "17:00", missed: value?.missed ?? "keep" });
              else onChange({ kind: "after_completion", every: 1, unit: "weeks", due_time: value?.due_time ?? "17:00", missed: value?.missed ?? "keep" });
            }}
          >
            <option value="none">Doesn't repeat</option>
            <option value="fixed">On a schedule</option>
            <option value="after_completion">After each one is done</option>
          </select>
        </label>
        {mode === "fixed" && (
          <label className="block sm:col-span-2">
            <span className="label">When</span>
            <select className="input" value={parsed.preset} onChange={(e) => setFixed({ preset: e.target.value as Preset })}>
              {PRESETS.map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
            </select>
          </label>
        )}
        {mode === "after_completion" && (
          <div className="flex items-end gap-2 sm:col-span-2">
            <label className="block w-20">
              <span className="label">Every</span>
              <input type="number" min={1} max={366} className="input" value={value?.every ?? 1} onChange={(e) => upd({ every: Math.max(1, Number(e.target.value)) })} />
            </label>
            <select className="input" aria-label="Unit" value={value?.unit ?? "weeks"} onChange={(e) => upd({ unit: e.target.value as Recurrence["unit"] })}>
              <option value="days">days</option>
              <option value="weeks">weeks</option>
              <option value="months">months</option>
              <option value="years">years</option>
            </select>
            <span className="pb-2 text-sm text-stone-500">after it's done</span>
          </div>
        )}
      </div>

      {mode === "fixed" && (parsed.preset === "weekly" || parsed.preset === "biweekly") && (
        <div className="flex flex-wrap gap-1">
          {DAY_SHORT.map((d, i) => {
            const on = parsed.days.includes(i);
            return (
              <button
                key={d}
                type="button"
                className={`rounded-md border px-2 py-1 text-xs ${on ? "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-700/30 dark:text-brand-100" : "border-stone-300 text-stone-400 dark:border-stone-700"}`}
                onClick={() => setFixed({ days: on ? parsed.days.filter((x) => x !== i) : [...parsed.days, i].sort() })}
              >
                {d}
              </button>
            );
          })}
        </div>
      )}
      {mode === "fixed" && parsed.preset === "monthly_day" && (
        <label className="flex items-center gap-2 text-sm">
          Day
          <input type="number" min={1} max={31} className="input !w-20" value={parsed.monthDay} onChange={(e) => setFixed({ monthDay: Math.min(31, Math.max(1, Number(e.target.value))) })} />
          of the month
        </label>
      )}
      {mode === "fixed" && parsed.preset === "monthly_nth" && (
        <div className="flex items-center gap-2 text-sm">
          <select className="input !w-28" aria-label="Which" value={parsed.nth} onChange={(e) => setFixed({ nth: e.target.value })}>
            {ORD.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
              </option>
            ))}
          </select>
          <select className="input !w-32" aria-label="Weekday" value={parsed.nthDay} onChange={(e) => setFixed({ nthDay: Number(e.target.value) })}>
            {DAY_SHORT.map((d, i) => (
              <option key={d} value={i}>
                {d}
              </option>
            ))}
          </select>
        </div>
      )}
      {mode === "fixed" && parsed.preset === "custom" && (
        <input
          className="input font-mono text-xs"
          placeholder="FREQ=MONTHLY;BYDAY=2TU"
          value={custom}
          onChange={(e) => {
            setCustom(e.target.value);
            upd({ kind: "fixed", rrule: e.target.value.replace(/^RRULE:/i, "") });
          }}
        />
      )}

      {mode !== "none" && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="label">Due at</span>
            <input type="time" className="input" value={value?.due_time ?? "17:00"} onChange={(e) => upd({ due_time: e.target.value })} />
          </label>
          <label className="block">
            <span className="label">On a weekend or holiday</span>
            <select className="input" value={value?.adjust ?? "none"} onChange={(e) => upd({ adjust: e.target.value as Recurrence["adjust"] })}>
              <option value="none">Keep the date</option>
              <option value="previous">Business day before</option>
              <option value="next">Business day after</option>
            </select>
          </label>
          <label className="block">
            <span className="label">If one is missed</span>
            <select className="input" value={value?.missed ?? "keep"} onChange={(e) => upd({ missed: e.target.value as Recurrence["missed"] })}>
              <option value="keep">Keep it open (overdue)</option>
              <option value="skip">Skip it</option>
              <option value="latest_only">Keep only the newest</option>
            </select>
          </label>
        </div>
      )}
      {err && <div className="text-xs text-red-600">{err}</div>}
      {preview && preview.dates.length > 0 && (
        <div className="text-xs text-stone-500">
          {mode === "after_completion" ? "If done today, next due " : "Next due "}
          {preview.dates.map((d) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })).join(" · ")}
          {preview.lead_days > 0 && <> · work can start {preview.lead_days} day{preview.lead_days === 1 ? "" : "s"} before each</>}
        </div>
      )}
      {seriesId && (
        <a className="text-xs text-brand-600 hover:underline" href={`/repeating#${seriesId}`}>
          Manage all occurrences of this repeating task →
        </a>
      )}
    </div>
  );
}
