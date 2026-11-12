"use client";

import { useEffect, useRef, useState } from "react";
import { api, bus } from "@/lib/api";
import type { Task } from "@/lib/types";
import { useApp } from "./Shell";

interface Preview {
  parsed: Record<string, unknown>;
  understood: string[];
  task: Task | null;
}

const EXAMPLES = [
  "Prepare CLE proposal by Friday, high priority, 3 hours",
  "Review QuickBooks every Friday for 30 minutes",
  "Email Laura tomorrow, 10 minutes",
  "Work on trauma presentation for 90 minutes three times this week",
];

export function QuickAdd({ onClose }: { onClose: () => void }) {
  const { openTask } = useApp();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [saved, setSaved] = useState<Task[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => inputRef.current?.focus(), []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    if (!text.trim()) {
      setPreview(null);
      return;
    }
    const t = setTimeout(async () => {
      try {
        setPreview(await api<Preview>("/tasks/quick-add", { method: "POST", json: { text, preview: true } }));
      } catch {}
    }, 200);
    return () => clearTimeout(t);
  }, [text]);

  async function save() {
    if (!text.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      const r = await api<Preview>("/tasks/quick-add", { method: "POST", json: { text } });
      if (r.task) setSaved((s) => [r.task!, ...s]);
      setText("");
      setPreview(null);
      bus.emit("tasks");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh]" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="w-full max-w-xl rounded-xl border border-stone-200 bg-white shadow-2xl dark:border-stone-700 dark:bg-stone-900">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
          className="p-3"
        >
          <input
            ref={inputRef}
            className="w-full bg-transparent px-1 py-2 text-base outline-none"
            placeholder="What needs doing? e.g. “Draft settlement memo by Tuesday, 2 hours, urgent”"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </form>
        <div className="border-t border-stone-100 px-4 py-3 text-sm dark:border-stone-800">
          {preview ? (
            <div className="space-y-2">
              <div className="font-medium">{String(preview.parsed.title)}</div>
              <div className="flex flex-wrap gap-1.5">
                {preview.understood.length === 0 && <span className="text-xs text-stone-400">No dates, durations or priorities found; it will be saved to your inbox as written.</span>}
                {preview.understood.map((u) => (
                  <span key={u} className="chip bg-brand-50 text-brand-700 dark:bg-brand-700/30 dark:text-brand-100">
                    {u}
                  </span>
                ))}
                {preview.parsed.category_name ? <span className="chip bg-stone-100 dark:bg-stone-800">{String(preview.parsed.category_name)}</span> : null}
              </div>
              <div className="text-xs text-stone-400">Press Enter to add to your inbox. Recurring rules are saved now and start generating occurrences in Phase 3.</div>
            </div>
          ) : (
            <div className="space-y-1 text-xs text-stone-500">
              <div>Try:</div>
              {EXAMPLES.map((ex) => (
                <button key={ex} className="block text-left hover:text-brand-600" onClick={() => setText(ex)}>
                  “{ex}”
                </button>
              ))}
            </div>
          )}
          {error && <div className="mt-2 text-xs text-red-600">{error}</div>}
        </div>
        {saved.length > 0 && (
          <div className="border-t border-stone-100 px-4 py-2 dark:border-stone-800">
            {saved.map((t) => (
              <button key={t.id} className="block w-full truncate py-0.5 text-left text-xs text-emerald-700 hover:underline dark:text-emerald-400" onClick={() => (onClose(), openTask(t.id))}>
                ✓ Added “{t.title}”, open to edit
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
