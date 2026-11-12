"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { bus, useApi } from "@/lib/api";
import { addDays, DAY_NAMES, fmtTime, sameDay, startOfDay, startOfWeek } from "@/lib/dates";
import type { CalEvent } from "@/lib/types";

export type CalMode = "day" | "week" | "month";
const HOUR_PX = 48;

interface Placed {
  ev: CalEvent;
  start: Date;
  end: Date;
  col: number;
  cols: number;
}

/** Side-by-side layout for overlapping events within one day. */
function layoutDay(events: CalEvent[], day: Date): Placed[] {
  const dayStart = startOfDay(day);
  const dayEnd = addDays(dayStart, 1);
  const items = events
    .filter((e) => !e.all_day)
    .map((ev) => ({ ev, start: new Date(Math.max(new Date(ev.start_at).getTime(), dayStart.getTime())), end: new Date(Math.min(new Date(ev.end_at).getTime(), dayEnd.getTime())) }))
    .filter((x) => x.end > x.start)
    .sort((a, b) => a.start.getTime() - b.start.getTime() || b.end.getTime() - a.end.getTime());
  const out: Placed[] = [];
  let cluster: Placed[] = [];
  let clusterEnd = 0;
  const flush = () => {
    const cols = Math.max(1, ...cluster.map((p) => p.col + 1));
    cluster.forEach((p) => (p.cols = cols));
    out.push(...cluster);
    cluster = [];
  };
  for (const it of items) {
    if (cluster.length && it.start.getTime() >= clusterEnd) flush();
    const used = new Set(cluster.filter((p) => p.end > it.start).map((p) => p.col));
    let col = 0;
    while (used.has(col)) col++;
    cluster.push({ ...it, col, cols: 1 });
    clusterEnd = Math.max(clusterEnd, it.end.getTime());
  }
  flush();
  return out;
}

function eventStyle(ev: CalEvent): React.CSSProperties {
  const c = ev.color ?? "#64748b";
  return ev.is_busy
    ? { background: `${c}26`, borderLeft: `3px solid ${c}` }
    : { background: `repeating-linear-gradient(135deg, ${c}14 0 6px, transparent 6px 12px)`, borderLeft: `3px dashed ${c}` };
}

function EventChip({ ev, compact }: { ev: CalEvent; compact?: boolean }) {
  return (
    <a
      href={ev.html_link ?? undefined}
      target="_blank"
      rel="noreferrer"
      title={`${ev.summary}\n${ev.calendar_name ?? ""}${ev.is_busy ? "" : " · free"}${ev.read_only ? " · read-only" : ""}`}
      className="block truncate rounded px-1 py-0.5 text-[11px] leading-tight text-stone-800 dark:text-stone-100"
      style={eventStyle(ev)}
    >
      {!compact && !ev.all_day && <span className="mr-1 text-stone-500">{fmtTime(new Date(ev.start_at))}</span>}
      {ev.summary}
    </a>
  );
}

export function CalendarView() {
  const [mode, setMode] = useState<CalMode>("week");
  const [anchor, setAnchor] = useState(() => new Date());
  const [now, setNow] = useState(() => new Date());
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const narrow = window.matchMedia("(max-width: 640px)").matches;
    if (narrow) setMode("day");
    const t = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(t);
  }, []);

  const { start, days } = useMemo(() => {
    if (mode === "day") return { start: startOfDay(anchor), days: 1 };
    if (mode === "week") return { start: startOfWeek(anchor), days: 7 };
    const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
    const s = startOfWeek(first);
    const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0);
    const span = Math.round((addDays(startOfWeek(last), 7).getTime() - s.getTime()) / 86400000);
    return { start: s, days: span };
  }, [mode, anchor]);
  const end = addDays(start, days);
  const { data: events, reload } = useApi<CalEvent[]>(`/events?start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`);
  useEffect(() => bus.on("calendars", reload), [reload]);

  useEffect(() => {
    if (scrollRef.current && mode !== "month") scrollRef.current.scrollTop = HOUR_PX * 7.5;
  }, [mode]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input, textarea, select")) return;
      if (e.key === "t") setAnchor(new Date());
      if (e.key === "d") setMode("day");
      if (e.key === "w") setMode("week");
      if (e.key === "m") setMode("month");
      if (e.key === "ArrowLeft") step(-1);
      if (e.key === "ArrowRight") step(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function step(dir: number) {
    if (mode === "day") setAnchor((a) => addDays(a, dir));
    else if (mode === "week") setAnchor((a) => addDays(a, 7 * dir));
    else setAnchor((a) => new Date(a.getFullYear(), a.getMonth() + dir, 1));
  }

  const dayList = Array.from({ length: days }, (_, i) => addDays(start, i));
  const evs = events ?? [];
  const allDayFor = (d: Date) =>
    evs.filter((e) => e.all_day && e.start_date && e.end_date && d >= new Date(e.start_date + "T00:00") && d < new Date(e.end_date + "T00:00"));

  const title =
    mode === "month"
      ? anchor.toLocaleDateString([], { month: "long", year: "numeric" })
      : mode === "day"
        ? anchor.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })
        : `${start.toLocaleDateString([], { month: "short", day: "numeric" })} – ${addDays(start, 6).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" })}`;

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-stone-200 px-3 py-2 dark:border-stone-800">
        <button className="btn" onClick={() => setAnchor(new Date())} title="Today (T)">
          Today
        </button>
        <button className="rounded p-1 hover:bg-stone-100 dark:hover:bg-stone-800" onClick={() => step(-1)} aria-label="Previous">
          ‹
        </button>
        <button className="rounded p-1 hover:bg-stone-100 dark:hover:bg-stone-800" onClick={() => step(1)} aria-label="Next">
          ›
        </button>
        <h2 className="text-sm font-semibold">{title}</h2>
        <div className="ml-auto flex rounded-md border border-stone-300 text-xs dark:border-stone-700">
          {(["day", "week", "month"] as const).map((m) => (
            <button key={m} onClick={() => setMode(m)} className={`px-2.5 py-1 capitalize ${mode === m ? "bg-stone-200 font-medium dark:bg-stone-700" : ""}`}>
              {m}
            </button>
          ))}
        </div>
      </div>

      {mode === "month" ? (
        <div className="grid flex-1 grid-cols-7 overflow-y-auto text-xs" style={{ gridTemplateRows: "auto", gridAutoRows: "minmax(90px, 1fr)" }}>
          {DAY_NAMES.map((d) => (
            <div key={d} className="sticky top-0 z-10 border-b border-stone-200 bg-white py-1 text-center font-medium text-stone-500 dark:border-stone-800 dark:bg-stone-900" style={{ gridRow: 1, minHeight: 0 }}>
              {d}
            </div>
          ))}
          {dayList.map((d) => {
            const list = [...allDayFor(d), ...evs.filter((e) => !e.all_day && sameDay(new Date(e.start_at), d))];
            return (
              <div key={d.toISOString()} className={`border-b border-r border-stone-100 p-1 dark:border-stone-800 ${d.getMonth() !== anchor.getMonth() ? "bg-stone-50 text-stone-400 dark:bg-stone-900/50" : ""}`}>
                <button className={`mb-0.5 rounded-full px-1.5 ${sameDay(d, now) ? "bg-brand-600 text-white" : ""}`} onClick={() => (setAnchor(d), setMode("day"))}>
                  {d.getDate()}
                </button>
                <div className="space-y-0.5">
                  {list.slice(0, 3).map((e) => (
                    <EventChip key={e.id + d.toISOString()} ev={e} compact />
                  ))}
                  {list.length > 3 && <div className="text-stone-400">+{list.length - 3} more</div>}
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        <>
          <div className="flex border-b border-stone-200 dark:border-stone-800">
            <div className="w-12 shrink-0" />
            {dayList.map((d) => (
              <div key={d.toISOString()} className="min-w-0 flex-1 border-l border-stone-100 px-1 py-1 dark:border-stone-800">
                <div className={`text-center text-xs ${sameDay(d, now) ? "font-semibold text-brand-600" : "text-stone-500"}`}>
                  {DAY_NAMES[(d.getDay() + 6) % 7]} {d.getDate()}
                </div>
                <div className="space-y-0.5">
                  {allDayFor(d).map((e) => (
                    <EventChip key={e.id} ev={e} compact />
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div ref={scrollRef} className="relative flex flex-1 overflow-y-auto">
            <div className="w-12 shrink-0">
              {Array.from({ length: 24 }, (_, h) => (
                <div key={h} className="relative text-right text-[10px] text-stone-400" style={{ height: HOUR_PX }}>
                  <span className="absolute -top-1.5 right-1">{h === 0 ? "" : new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" })}</span>
                </div>
              ))}
            </div>
            {dayList.map((d) => (
              <div key={d.toISOString()} className="relative min-w-0 flex-1 border-l border-stone-100 dark:border-stone-800" style={{ height: HOUR_PX * 24 }}>
                {Array.from({ length: 24 }, (_, h) => (
                  <div key={h} className="border-t border-stone-100 dark:border-stone-800/70" style={{ height: HOUR_PX }} />
                ))}
                {layoutDay(evs, d).map((p) => {
                  const top = ((p.start.getTime() - startOfDay(d).getTime()) / 3600000) * HOUR_PX;
                  const h = Math.max(18, ((p.end.getTime() - p.start.getTime()) / 3600000) * HOUR_PX - 2);
                  return (
                    <a
                      key={p.ev.id}
                      href={p.ev.html_link ?? undefined}
                      target="_blank"
                      rel="noreferrer"
                      className="absolute overflow-hidden rounded-md px-1.5 py-0.5 text-[11px] leading-tight text-stone-800 hover:z-10 hover:shadow dark:text-stone-100"
                      style={{ ...eventStyle(p.ev), top, height: h, left: `calc(${(p.col / p.cols) * 100}% + 2px)`, width: `calc(${100 / p.cols}% - 4px)` }}
                      title={`${p.ev.summary}\n${fmtTime(p.start)} – ${fmtTime(p.end)}\n${p.ev.calendar_name ?? ""}${p.ev.is_busy ? " · busy" : " · free"}${p.ev.read_only ? " · read-only" : ""}`}
                    >
                      <div className="truncate font-medium">{p.ev.summary}</div>
                      {h > 30 && (
                        <div className="truncate text-stone-500 dark:text-stone-400">
                          {fmtTime(p.start)} – {fmtTime(p.end)}
                        </div>
                      )}
                    </a>
                  );
                })}
                {sameDay(d, now) && (
                  <div className="pointer-events-none absolute inset-x-0 z-20 border-t-2 border-red-500" style={{ top: ((now.getTime() - startOfDay(now).getTime()) / 3600000) * HOUR_PX }}>
                    <div className="-mt-[5px] -ml-[5px] h-2 w-2 rounded-full bg-red-500" />
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
